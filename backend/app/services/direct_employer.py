"""Direct-employer discovery: turn a list of UK employer domains into ATS board
tokens the existing discovery tier already knows how to fetch.

WHY THIS SHAPE
The obvious reading of "crawl employers' own vacancies pages" is a bespoke HTML
job parser per site, which is thousands of layouts, permanently breaking, and
produces exactly the text-starved rows the pipeline spent months fixing. It is
also unnecessary for most employers, because they do not host their vacancies
themselves -- they link out to a hosted ATS board. So this crawl does not parse
jobs at all. It reads an employer's careers page, works out WHICH ATS they use,
and writes the (vendor, token) into `company_ats`.

From there nothing else changes: fetch_ats already returns clean, text-complete
postings for that board on every run, with structured dates and locations. One
crawl converts an employer into a first-class discovery source permanently,
rather than into one scrape that rots.

That also means the crawl's OUTPUT is small and cheap even though its INPUT is
large: 8,000 domains in, some hundreds of board tokens out, and the per-search
cost afterwards is zero (the ATS batch is rotated and TTL-cached as before).

WHAT IT COSTS
Per domain: 1 robots.txt + 1 homepage + up to CAREERS_PAGES_PER_SITE careers
pages, all plain HTTP with no browser and no LLM, plus one live validation call
per candidate token found. Concurrency is spread ACROSS domains, not against
one host -- each individual site sees at most a handful of sequential requests,
which is why a modest worker count is polite here rather than aggressive.

robots.txt is honoured. This is the one part of the app that fetches arbitrary
third-party sites that never asked to be crawled, so it asks first and records
`blocked_by_robots` rather than quietly proceeding.

NOT A SEARCH-TIME PATH. Nothing here runs during a search -- see
services/scheduler.py and the /admin/crawl endpoint. A pass over the whole seed
list is tens of minutes of wall time; putting it anywhere near the request path
or the search critical path would be a straight regression.
"""
from __future__ import annotations

import os
import re
import threading
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit

import requests
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import DirectEmployerProbe

# Identify ourselves honestly: a charity webmaster seeing this in their logs can
# tell what it is and who to contact, which is the difference between a crawler
# and a nuisance.
USER_AGENT = os.getenv(
    "CRAWL_USER_AGENT",
    "FourInAThousandBot/1.0 (+job-search aggregator; respects robots.txt)",
)

CRAWL_WORKERS = int(os.getenv("CRAWL_WORKERS", "8"))
CRAWL_TIMEOUT = float(os.getenv("CRAWL_TIMEOUT", "10"))
CAREERS_PAGES_PER_SITE = int(os.getenv("CRAWL_CAREERS_PAGES", "2"))
# How long a probe result stands before the domain is eligible again. An
# employer changes ATS rarely, and re-probing 8k domains weekly would be a lot
# of other people's bandwidth for almost no new information.
RECHECK_DAYS = int(os.getenv("CRAWL_RECHECK_DAYS", "120"))
# A page bigger than this is not a careers page; stop reading rather than pull
# a whole media-heavy homepage into memory.
MAX_PAGE_BYTES = 600_000

# Tag written to company_ats.keyword. select_ats_batch_for_run matches a
# profile's terms/sectors against this word-wise, so it carries the words a
# candidate targeting this vertical would actually have on their profile --
# a bare "charity" would only ever match a profile using that exact word.
CHARITY_KEYWORD = "charity nonprofit voluntary"

# Anchor text / hrefs that mean "this way to the jobs". Ordered by how specific
# they are: "work-for-us" is almost never anything else, "opportunities" often
# is (funding opportunities, volunteering opportunities), so it ranks last.
_CAREERS_HINTS = [
    "work-for-us", "work-with-us", "workforus", "join-our-team", "join-the-team",
    "job-vacancies", "current-vacancies", "vacancies", "careers", "career",
    "jobs", "recruitment", "join-us", "work-here", "opportunities",
]
_HREF_RE = re.compile(r"""<a\b[^>]*?href\s*=\s*["']([^"'#]+)["'][^>]*>(.*?)</a>""",
                      re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# Tried only when the homepage yielded no careers link at all -- which a measured
# sample put at ~26% of domains, mostly because the nav is rendered by JavaScript
# we never execute. Cheap and bounded: at most two extra GETs on conventional
# paths, and only for sites that gave us nothing to follow.
_COMMON_CAREERS_PATHS = ("/jobs", "/careers", "/vacancies", "/about-us/jobs")
_COMMON_PATH_TRIES = int(os.getenv("CRAWL_COMMON_PATH_TRIES", "2"))

# Recruitment platforms we do NOT support. Recording which one a miss was using
# is the whole reason a `no_ats` row is worth storing: it turns the crawl's
# failures into the evidence for which vendor to integrate next, instead of
# leaving "no ATS found" indistinguishable from "uses something we can't read".
# Matched against external hosts linked from the careers page.
_FOREIGN_ATS_HOSTS = (
    "myworkdayjobs.com", "myworkday.com", "successfactors.eu", "successfactors.com",
    "recruitmentplatform.com", "current-vacancies.com", "jobtrain.co.uk",
    "hireful.co.uk", "networxrecruitment.com", "eploy.net", "tribepad.com",
    "icims.com", "taleo.net", "brassring.com", "peoplehr.net", "breathehr.com",
    "bamboohr.com", "jobvite.com", "workforcenow.adp.com", "jobteaser.com",
    "applicantpro.com", "teamtailor.com", "pinpointhq.com", "occupop.com",
    "hireroad.com", "cezanneondemand.com", "webrecruit.co.uk", "vacancyfiller.co.uk",
)
_FOREIGN_ATS_RE = re.compile(
    "|".join(re.escape(h) for h in _FOREIGN_ATS_HOSTS), re.I)

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_robots_lock = threading.Lock()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT,
                      "Accept": "text/html,application/xhtml+xml"})
    return s


def _robots_allows(sess: requests.Session, domain: str, path: str) -> bool:
    """True if robots.txt permits `path`. Fetched once per domain and cached.

    Fails OPEN on a missing or unreadable robots.txt, which is the standard
    reading (no robots.txt means no restrictions), and fails CLOSED only on an
    explicit disallow."""
    with _robots_lock:
        cached = _robots_cache.get(domain, "miss")
    if cached == "miss":
        parser: urllib.robotparser.RobotFileParser | None = None
        try:
            r = sess.get(f"https://{domain}/robots.txt", timeout=CRAWL_TIMEOUT)
            if r.status_code == 200 and len(r.text) < 200_000:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(r.text.splitlines())
        except Exception:
            parser = None
        with _robots_lock:
            _robots_cache[domain] = parser
        cached = parser
    if cached is None:
        return True
    try:
        return cached.can_fetch(USER_AGENT, path)
    except Exception:
        return True


def _get(sess: requests.Session, url: str) -> str | None:
    """Fetch one page as text, or None. Streamed so an unexpectedly huge
    response is abandoned rather than buffered whole."""
    try:
        r = sess.get(url, timeout=CRAWL_TIMEOUT, allow_redirects=True, stream=True)
        if r.status_code != 200:
            return None
        ctype = (r.headers.get("content-type") or "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            return None
        body = r.raw.read(MAX_PAGE_BYTES, decode_content=True) or b""
        return body.decode(r.encoding or "utf-8", errors="replace")
    except Exception:
        return None
    finally:
        try:
            r.close()
        except Exception:
            pass


def _careers_links(html: str, base_url: str) -> list[str]:
    """Candidate careers-page URLs from a homepage, most promising first.

    Matches on BOTH the href and the anchor text, because the two fail in
    opposite directions: a link reading "Work for us" often points at
    /about/12345, and a link to /careers is often an icon with no text."""
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()
    base_host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    for href, inner in _HREF_RE.findall(html or ""):
        text = _TAG_RE.sub(" ", inner or "")
        haystack = f"{href} {text}".lower()
        best = None
        for rank, hint in enumerate(_CAREERS_HINTS):
            if hint in haystack:
                best = rank
                break
        if best is None:
            continue
        url = urljoin(base_url, href.strip())
        if not url.startswith(("http://", "https://")):
            continue
        host = urlsplit(url).netloc.lower().removeprefix("www.")
        # An off-site careers link is usually the ATS board itself, which is
        # exactly what we want -- keep it. ats_tokens_in reads it directly.
        if url in seen:
            continue
        seen.add(url)
        # Prefer same-host pages; an off-site link that is NOT an ATS we know
        # about is usually a generic job board and a waste of a fetch.
        offsite_penalty = 0 if host == base_host or host.endswith("." + base_host) else 100
        scored.append((best + offsite_penalty, url))
    scored.sort(key=lambda x: x[0])
    return [u for _rank, u in scored]


def _probe_domain(domain: str, company: str) -> dict:
    """Look for an ATS board for one employer. Pure network + parsing, NO DB --
    this runs on a worker thread, and every write happens back on the caller's
    thread (the same rule the search pipeline's cluster workers follow)."""
    import full_auto as engine  # lazy: heavy import, and this is off the hot path

    sess = _session()
    result = {"domain": domain, "company": company, "status": "no_ats",
              "vendor": None, "token": None, "careers_url": None, "note": None}

    if not _robots_allows(sess, domain, "/"):
        result.update(status="blocked_by_robots", note="robots.txt disallows /")
        return result

    home_url = f"https://{domain}/"
    html = _get(sess, home_url)
    if html is None:
        result.update(status="unreachable", note="homepage fetch failed")
        return result

    # The board is often linked straight from the footer, so check the homepage
    # itself before spending a second fetch.
    pages: list[tuple[str, str]] = [(home_url, html)]
    candidates = _careers_links(html, home_url)
    if not candidates:
        candidates = [urljoin(home_url, p) for p in _COMMON_CAREERS_PATHS[:_COMMON_PATH_TRIES]]
        result["note"] = "no careers link on homepage; tried common paths"
    for url in candidates[:CAREERS_PAGES_PER_SITE]:
        path = urlsplit(url).path or "/"
        if urlsplit(url).netloc.lower().removeprefix("www.") == domain \
                and not _robots_allows(sess, domain, path):
            continue
        # An off-site link that is already a recognisable board URL needs no
        # fetch at all -- the token is in the href.
        if engine.ats_tokens_in(url):
            pages.append((url, url))
            continue
        page = _get(sess, url)
        if page:
            pages.append((url, page))

    for url, text in pages:
        for vendor, token in engine.ats_tokens_in(text):
            # Extraction says a page LINKS to a board; validation says the board
            # is real and has open roles. Only the second one earns a store row.
            if engine.validate_ats_token(vendor, token) >= 1:
                result.update(status="ats_found", vendor=vendor, token=token,
                              careers_url=url, note=None)
                return result

    # Missed -- but record WHY, so the misses accumulate into a ranked case for
    # which platform to support next rather than into an undifferentiated pile.
    for url, text in pages:
        m = _FOREIGN_ATS_RE.search(text or "")
        if m:
            result.update(note=f"unsupported ATS: {m.group(0).lower()}",
                          careers_url=url)
            break
    return result


def _due_domains(db: Session, seed: list[tuple], limit: int) -> list[tuple]:
    """The next `limit` (name, domain) seed pairs with no probe inside the
    recheck window. Seed order is income-descending, so a partial pass always
    spends its budget on the biggest employers left."""
    cutoff = datetime.utcnow() - timedelta(days=RECHECK_DAYS)
    fresh = {
        d for (d,) in db.execute(
            select(DirectEmployerProbe.domain).where(DirectEmployerProbe.probed_at >= cutoff)
        ).all()
    }
    out: list[tuple] = []
    for name, domain in seed:
        if domain in fresh:
            continue
        out.append((name, domain))
        if len(out) >= limit:
            break
    return out


def _record(db: Session, res: dict, source_list: str) -> None:
    row = db.execute(
        select(DirectEmployerProbe).where(DirectEmployerProbe.domain == res["domain"])
    ).scalar_one_or_none()
    if row is None:
        row = DirectEmployerProbe(domain=res["domain"])
        db.add(row)
    row.company = res["company"]
    row.source_list = source_list
    row.status = res["status"]
    row.vendor = res["vendor"]
    row.token = res["token"]
    row.careers_url = res["careers_url"]
    row.note = res["note"]
    row.probed_at = datetime.utcnow()


def crawl_uk_charities(limit: int = 200, workers: int | None = None) -> dict:
    """Probe the next `limit` unprobed UK charity domains and store what's found.

    Returns a summary including the HIT RATE, which is the number that decides
    whether this vertical is worth continuing to crawl -- not the raw count of
    tokens added."""
    import full_auto as engine

    from ..uk_charity_gen import CHARITIES

    db = SessionLocal()
    try:
        seed = [(name, domain) for (name, domain, _inc, _num, _pc) in CHARITIES]
        todo = _due_domains(db, seed, max(0, limit))
        if not todo:
            return {"probed": 0, "remaining": 0, "note": "nothing due"}

        with ThreadPoolExecutor(max_workers=max(1, workers or CRAWL_WORKERS)) as ex:
            results = list(ex.map(lambda r: _probe_domain(r[1], r[0]), todo))

        added: list[tuple] = []
        counts: dict[str, int] = {}
        for res in results:
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            _record(db, res, "uk_charity")
            if res["status"] == "ats_found":
                added.append((res["company"], res["vendor"], res["token"], CHARITY_KEYWORD))
        db.commit()

        # One write, after the DB commit: save_company_ats opens its own raw
        # sqlite connection to the same file, so it must not run inside this
        # session's transaction.
        if added:
            engine.save_company_ats(added, default_keyword=CHARITY_KEYWORD)

        probed_total = db.execute(select(func.count(DirectEmployerProbe.id))).scalar_one()
        hits = counts.get("ats_found", 0)
        return {
            "probed": len(results),
            "ats_found": hits,
            "hit_rate": round(hits / len(results), 4) if results else 0.0,
            "by_status": counts,
            "vendors": _vendor_mix(added),
            "probed_all_time": probed_total,
            "remaining": max(0, len(seed) - probed_total),
        }
    finally:
        db.close()


def _vendor_mix(added: list[tuple]) -> dict[str, int]:
    mix: dict[str, int] = {}
    for _company, vendor, _token, _kw in added:
        mix[vendor] = mix.get(vendor, 0) + 1
    return mix


def _charity_board_yield(db: Session) -> dict:
    """What the charity-sourced boards have actually produced downstream.

    The crawl's own hit rate measures how many BOARDS were found, which is not
    the question -- listings only pay if they get far enough down the pipeline
    to be shown. A board contributing 400 listings that all die at the embedding
    pre-filter is worth less than one contributing 3 that get surfaced, so
    counting discovery volume alone would flatter this whole strategy.

    Deliberately a read-side join over existing columns rather than a new funnel
    counter: it costs the search path nothing, which matters for a feature whose
    measured board yield is small."""
    from ..models import CompanyATS, JobSeen, Role
    from .sources import SOURCES

    prefix_for = {s["key"]: s["board_prefix"] for s in SOURCES}
    tokens = db.execute(
        select(CompanyATS.vendor, CompanyATS.token)
        .where(CompanyATS.keyword == CHARITY_KEYWORD)
    ).all()
    sources = [f"{prefix_for.get(v, v)}:{t}" for v, t in tokens]
    if not sources:
        return {"boards": 0, "discovered": 0, "gated": 0, "shown": 0, "saved_or_applied": 0}

    def _count(model, column, extra=None):
        stmt = select(func.count(model.id)).where(column.in_(sources))
        if extra is not None:
            stmt = stmt.where(extra)
        return int(db.execute(stmt).scalar_one() or 0)

    return {
        "boards": len(sources),
        "discovered": _count(JobSeen, JobSeen.source),
        "gated": _count(JobSeen, JobSeen.source, JobSeen.state.in_(["enriched", "shown"])),
        "shown": _count(JobSeen, JobSeen.source, JobSeen.state == "shown"),
        "saved_or_applied": _count(Role, Role.source, Role.status.in_(["saved", "applied"])),
    }


def crawl_status() -> dict:
    """Read-only progress + hit rate over every probe recorded so far."""
    from ..uk_charity_gen import CHARITIES

    db = SessionLocal()
    try:
        by_status = {
            s: n for s, n in db.execute(
                select(DirectEmployerProbe.status, func.count(DirectEmployerProbe.id))
                .group_by(DirectEmployerProbe.status)
            ).all()
        }
        by_vendor = {
            v: n for v, n in db.execute(
                select(DirectEmployerProbe.vendor, func.count(DirectEmployerProbe.id))
                .where(DirectEmployerProbe.status == "ats_found")
                .group_by(DirectEmployerProbe.vendor)
            ).all()
        }
        probed = sum(by_status.values())
        hits = by_status.get("ats_found", 0)
        return {
            "seed_size": len(CHARITIES),
            "probed": probed,
            "remaining": max(0, len(CHARITIES) - probed),
            "ats_found": hits,
            "hit_rate": round(hits / probed, 4) if probed else 0.0,
            "by_status": by_status,
            "by_vendor": by_vendor,
            # The number that decides whether the vertical is worth continuing.
            "yield": _charity_board_yield(db),
        }
    finally:
        db.close()
