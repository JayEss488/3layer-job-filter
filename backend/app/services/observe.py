"""Scheduled listing observation: the collection half of ghost-job detection.

WHAT THIS IS FOR
The strongest ghost-listing signals are longitudinal -- how long an ad has been
standing, whether it is reposted, whether it came down and went back up. None of
them can be computed from a snapshot, and none can be reconstructed after the
fact: a week not observed is a week permanently missing. Meanwhile the app's
only source of observations was user-initiated searches, which on a measured
store ran on 9 distinct dates in 12 days with a 5-day hole in the middle. Under
that cadence `seen_days / observed_days` -- the density that separates a standing
pipeline ad from a genuine repost -- is measuring the user's habits, not the ad.

So this runs discovery on a schedule, independently of anyone searching, and
records only WHEN each listing was seen.

DAILY IS A FUNCTIONAL REQUIREMENT, NOT A PREFERENCE.
full_auto.EVERGREEN_SEEN_DENSITY (0.6) asks what fraction of the days since we
first saw an ad we have seen it again. Miss days and the denominator keeps
growing while the numerator does not, so a gap doesn't merely delay the signal
by the days lost -- it actively pushes a genuine evergreen ad below the
threshold. A 5-day gap costs more than 5 days.

WHY THERE IS NO IN-PROCESS SCHEDULER
Same reasoning as scripts/crawl_direct_employers.py: a timer thread inside the
single-instance box whose SQLite file is the app's only store is infrastructure
risk with no matching payoff. The schedulable units are
scripts/observe_listings.py and POST /admin/observe; point an external scheduler
at one of them.

WHAT IT COSTS
Board-API quota only. No OpenAI call of any kind -- no embeddings, no gate, no
rank, no judge. OBSERVE_SOURCES defaults to reed+adzuna, both plain structured
APIs on the free tier.

THE SAFETY CONTRACT, which is enforced by one fact
This module never writes to `jobs_seen`. Everything else follows from that and
needs no separate guard: no row is marked `enriched` (nothing calls
engine._mark), no Role row is created, no embedding is computed, and no
gate/rank/judge call happens. It also never creates a SearchRun row, which is
precisely why it cannot consume MAX_SEARCHES_PER_DAY -- routers/search.py's
_searches_today counts SearchRun joined to Profile. There is nothing to
configure here; the guarantee is that the writes simply do not exist.

WHAT AN OBSERVATION PROVES, AND WHAT IT DOES NOT
Re-finding a listing through a board's SEARCH api proves it is still INDEXED,
not that it is still LIVE. That is the same lower bound JobSeen.seen_days
already carries. recheck_liveness() below is what upgrades a sighting into a
direct answer, and it is the half that produces `dead_at`.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

from sqlalchemy import bindparam, func, select, update
from sqlalchemy.orm import Session

from ..models import JobSeen, ListingObservation, ProfileAttribute, Role, RoleFamily
from .moderation import filter_blocked, get_blocked_domains
from .sources import ATS_KEYS, SOURCES

# Which discovery sources the observation pass is allowed to use. Deliberately
# narrow, and narrow for cost reasons that differ per source:
#   * google_jobs spends serper.dev credits per query;
#   * jsearch spends RapidAPI quota;
#   * careerjet's URLs are opaque jobviewtrack.com/v2/<blob> redirects that
#     carry no re-findable id (full_auto._board_ref returns None for every one),
#     so its rows cannot hold a stable observation window anyway -- observing it
#     would generate history that re-keys itself and reads as churn;
#   * usajobs self-gates to nothing outside the US.
# Reed and Adzuna are free, structured, country-scoped and -- measured on the
# live store -- where most strongly-ranked picks actually come from.
OBSERVE_SOURCES = {
    s.strip() for s in os.getenv("OBSERVE_SOURCES", "reed,adzuna").split(",") if s.strip()
}
# Term budget per pass. Each term is one task per source in gather_jobs' 12-wide
# pool, so this is closer to a breadth knob than a latency one.
OBSERVE_TERMS_PER_RUN = int(os.getenv("OBSERVE_TERMS_PER_RUN", "12"))
# How many already-observed listings get their title carried forward as a search
# term (see _observation_terms). Small: this is a top-up for continuity, not the
# main term source.
OBSERVE_CARRY_FORWARD = int(os.getenv("OBSERVE_CARRY_FORWARD", "4"))

_ALL_SOURCE_KEYS = {s["key"] for s in SOURCES}


def _disabled_for_observation() -> set[str]:
    """Every source key EXCEPT the allowed ones, plus every ATS vendor.

    Expressed as a deny-set rather than an allow-set because that is the only
    lever gather_jobs actually exposes (`profile["disabled_sources"]`), and
    routing through it means this module needs no changes in full_auto at all.

    The ATS batch is suppressed wholesale. Two reasons: select_ats_batch_for_run
    rotates a per-profile cursor that a background pass has no business
    advancing, and an ATS feed is a whole-board dump whose per-listing
    observation value is low relative to its fan-out. ATS rows get something
    strictly better from recheck_liveness() -- a vendor feed lists exactly the
    reqs that are open, so absence from it is DEFINITIVE, where a search-api
    sighting is only ever a lower bound.
    """
    return (_ALL_SOURCE_KEYS - OBSERVE_SOURCES) | set(ATS_KEYS)


def _observation_terms(db: Session, limit: int = OBSERVE_TERMS_PER_RUN) -> list[str]:
    """Search terms for a pass that belongs to no profile.

    Two halves, and the second is the load-bearing one.

    (1) The union of every profile's active target roles -- the population users
        actually search. A fixed generic term list was rejected: it would build a
        long, clean observation history of listings nobody will ever be shown,
        i.e. history that is never read.

    (2) The titles of listings ALREADY under observation and not yet known dead.
        Without this, a listing whose term stops being picked by the rotation
        simply vanishes from the window -- and a gap in seen_dates is
        indistinguishable from the ad coming down, which corrupts exactly the
        density calculation the evergreen rule depends on. Continuity of an
        existing window is worth more than breadth of a new one.

    Rotated deterministically by day-of-year rather than randomly so that every
    term is picked within a bounded cycle and two passes on the same day agree.
    """
    rows = db.execute(
        select(ProfileAttribute.value)
        .join(RoleFamily, ProfileAttribute.family_id == RoleFamily.id, isouter=True)
        .where(ProfileAttribute.type == "target_role")
        .where((RoleFamily.tier == "active") | (ProfileAttribute.family_id.is_(None)))
    ).scalars().all()
    seen: set[str] = set()
    targets: list[str] = []
    for v in rows:
        key = (v or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            targets.append(v.strip())

    # (2) carry-forward. Ordered by first_seen ASC: the oldest windows are the
    # ones with the most to lose from a gap, and the ones whose age is most
    # informative once a rule can read it.
    carried = db.execute(
        select(ListingObservation.title)
        .where(ListingObservation.dead_at.is_(None))
        .where(ListingObservation.repost_key.isnot(None))
        .order_by(ListingObservation.first_seen.asc())
        .limit(OBSERVE_CARRY_FORWARD * 8)
    ).scalars().all()
    carry: list[str] = []
    for t in carried:
        key = (t or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            carry.append(t.strip())
        if len(carry) >= OBSERVE_CARRY_FORWARD:
            break

    if not targets:
        return carry[:limit]
    # Rotate the target window by day-of-year so successive days explore
    # different terms while still returning to each within a bounded cycle.
    room = max(1, limit - len(carry))
    n = len(targets)
    if n <= room:
        window = list(targets)
    else:
        start = (datetime.utcnow().timetuple().tm_yday * room) % n
        window = [targets[(start + i) % n] for i in range(room)]
    return window + carry


def _synthetic_profile(db: Session, terms: list[str]) -> dict:
    """The minimal profile dict gather_jobs needs, belonging to nobody.

    profile_id = -1 is deliberate and load-bearing. full_auto._cursor_key scopes
    the shared source-rotation cursor by profile_id, so passing a real user's id
    here would advance THEIR term/ATS rotation from a background job -- their
    next search would silently query a different window because a crawl ran.
    -1 cannot collide with a real row and gives the crawl its own cursor.

    visa_sponsor_only is left unset so _sponsor_scoped_terms no-ops: sponsor
    scoping deepens a seam for one candidate, which is meaningless for a pass
    that belongs to no candidate.
    """
    return {
        "profile_id": -1,
        "search_terms": terms,
        "role_clusters": [],
        "disabled_sources": _disabled_for_observation(),
        # National scope with no place: _geo_scoped_location returns "" for
        # anything but "local", which is what makes Reed/Adzuna search the whole
        # country rather than locking onto one town.
        "location_scope": "national",
        "location": "",
        "adzuna_country_code": os.getenv("OBSERVE_COUNTRY", "gb"),
        "first_run": False,
    }


def _as_dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        from .engine import _parse_iso
        return _parse_iso(value)
    return None


def _upsert_observations(db: Session, jobs: list[dict]) -> dict:
    """Record today's sighting for every discovered listing.

    Continuity lookup order -- source_ref FIRST, identity_hash second. That
    ordering is the whole point of source_ref: identity_hash is
    sha1(_canonical_url(url)), so a board that re-slugs a listing (Reed does)
    or an aggregator that adds a tracking parameter mints a brand-new identity
    and restarts first_seen at zero. Measured on the live store, two listings
    had already done this in 12 days -- Reed job 57106990 appeared under both
    "lead-software-engineer" and "lead-oracle-applications-engineer", the second
    copy reporting itself as new while the first had a 9-day window. Keying on
    the board's own id first keeps the window intact.

    Writes ONLY to listing_observations. See the module docstring.
    """
    from . import engine as eng
    import full_auto as fa

    now = datetime.utcnow()
    stats = {"seen": 0, "new": 0, "refreshed": 0, "continued_by_ref": 0}

    # Pre-load the rows this batch could touch, keyed both ways, so the pass is
    # two indexed queries rather than one per job.
    idents, refs = set(), set()
    prepared = []
    for job in jobs:
        url = job.get("url") or ""
        if not url:
            continue
        ident = eng.identity_hash(job)
        ref = fa._board_ref(url)
        idents.add(ident)
        if ref:
            refs.add(ref)
        prepared.append((job, ident, ref))
    if not prepared:
        return stats

    by_ident: dict[str, ListingObservation] = {}
    by_ref: dict[str, ListingObservation] = {}
    for chunk in _chunks(sorted(idents), 400):
        for row in db.execute(select(ListingObservation)
                              .where(ListingObservation.identity_hash.in_(chunk))).scalars():
            by_ident[row.identity_hash] = row
    for chunk in _chunks(sorted(refs), 400):
        for row in db.execute(select(ListingObservation)
                              .where(ListingObservation.source_ref.in_(chunk))).scalars():
            by_ref.setdefault(row.source_ref, row)

    for job, ident, ref in prepared:
        stats["seen"] += 1
        row = by_ident.get(ident)
        if row is None and ref:
            row = by_ref.get(ref)
            if row is not None:
                stats["continued_by_ref"] += 1
        posted = _as_dt(job.get("posted_at"))
        expires = _as_dt(job.get("expires_at"))

        if row is None:
            row = ListingObservation(
                identity_hash=ident,
                source_ref=ref,
                source=job.get("board") or job.get("source"),
                company=job.get("company"),
                title=job.get("title"),
                url=job.get("url"),
                repost_key=eng._repost_key(job),
                posted_at=posted,
                posted_at_approx=bool(job.get("posted_at_approx")) if posted else None,
                expires_at=expires,
                first_seen=now,
                last_seen=now,
                seen_dates=eng._append_sighting(None, now),
            )
            db.add(row)
            by_ident[ident] = row
            if ref:
                by_ref.setdefault(ref, row)
            stats["new"] += 1
            continue

        # Existing window: extend it. _append_sighting is idempotent per UTC day,
        # so two passes on one day record one sighting -- unlike a naive counter
        # it does not depend on being sequenced before last_seen is overwritten.
        row.seen_dates = eng._append_sighting(row.seen_dates, now)
        row.last_seen = now
        if ref and not row.source_ref:
            row.source_ref = ref
        # Earliest claimed posted_at wins: an aggregator re-listing an old
        # posting must not be able to launder it fresh. Latest expiry wins: an
        # employer can genuinely extend a closing date. Same rule as
        # engine._merge_posted_expires, restated rather than imported because
        # that one takes a JobSeen row.
        if posted and (row.posted_at is None or posted < row.posted_at):
            row.posted_at = posted
            row.posted_at_approx = bool(job.get("posted_at_approx"))
        if expires and (row.expires_at is None or expires > row.expires_at):
            row.expires_at = expires
        if not row.repost_key:
            row.repost_key = eng._repost_key(job)
        stats["refreshed"] += 1

    db.commit()
    return stats


def _chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def backfill_source_refs(db: Session, batch: int = 5000) -> int:
    """Parse a board id onto observation rows that don't have one yet.

    Needed because rows seeded by database._migrate_listing_observations carry
    no source_ref: that migration runs inside init_db, and full_auto (where
    _board_ref lives) pulls in crawl4ai/playwright, so importing it there would
    mean the API could no longer boot without them. Doing it here instead costs
    nothing -- this module has already imported full_auto to crawl.

    Without it, continuity-by-ref cannot fire for any pre-existing row: a
    listing that re-slugs would still restart its window, which is the exact
    failure source_ref exists to prevent. Rows do self-heal on their next
    sighting (_upsert_observations fills a missing ref), but only if they are
    re-observed, and the ones most worth protecting are the ones drifting out
    of the term rotation.

    Idempotent and bounded: only touches rows where source_ref IS NULL, so it
    is a no-op once caught up."""
    import full_auto as fa

    rows = db.execute(
        select(ListingObservation.identity_hash, ListingObservation.url)
        .where(ListingObservation.source_ref.is_(None))
        .where(ListingObservation.url.isnot(None))
        .limit(batch)
    ).all()
    updates = [{"i": ident, "r": ref}
               for ident, url in rows
               if (ref := fa._board_ref(url or ""))]
    if not updates:
        return 0
    # Core-level table update: an executemany against the ORM entity tries to
    # synchronize persistent objects, which SQLAlchemy rejects outright when the
    # WHERE clause is parameterised per row.
    db.execute(
        update(ListingObservation.__table__)
        .where(ListingObservation.__table__.c.identity_hash == bindparam("i"))
        .values(source_ref=bindparam("r")),
        updates,
    )
    db.commit()
    return len(updates)


def run_pass(db: Session, limit_terms: int = OBSERVE_TERMS_PER_RUN) -> dict:
    """One observation pass: discover, filter, record sightings. No LLM, no
    Role rows, no jobs_seen writes, no SearchRun."""
    import full_auto as fa

    started = time.monotonic()
    backfilled = backfill_source_refs(db)
    terms = _observation_terms(db, limit_terms)
    if not terms:
        return {"ok": False, "reason": "no target roles to observe yet", "terms": []}

    profile = _synthetic_profile(db, terms)
    raw = fa.gather_jobs(profile) or []
    raw, n_blocked = filter_blocked(raw, get_blocked_domains(db))
    stats = _upsert_observations(db, raw)
    stats.update({
        "ok": True,
        "terms": profile.get("search_terms_batch") or terms,
        "discovered": len(raw),
        "blocked": n_blocked,
        "source_refs_backfilled": backfilled,
        "elapsed_s": round(time.monotonic() - started, 1),
    })
    print(f"[observe] {stats}")
    return stats


# How many listings one re-check pass will fetch. Plain GETs, no LLM, no API
# credits -- the ceiling is politeness and wall time, not spend.
OBSERVE_RECHECK_MAX_PER_PASS = int(os.getenv("OBSERVE_RECHECK_MAX_PER_PASS", "300"))
# Don't re-fetch a listing verified more recently than this.
OBSERVE_RECHECK_DAYS = int(os.getenv("OBSERVE_RECHECK_DAYS", "7"))
# Same width as engine._verify_listings_alive uses in-run.
OBSERVE_RECHECK_WORKERS = int(os.getenv("OBSERVE_RECHECK_WORKERS", "8"))


def recheck_liveness(db: Session, limit: int = OBSERVE_RECHECK_MAX_PER_PASS,
                     dry_run: bool = False) -> dict:
    """Directly confirm whether tracked listings still exist, and stamp dead_at.

    WHY THIS IS THE HIGH-VALUE HALF. A sighting from a board's search API is a
    lower bound and nothing more. `dead_at` -- the moment a listing stopped
    existing -- is what closes the bracket that first_seen opens, and it is the
    only way to answer "how long does a listing actually stay up", which is the
    central ghost-listing question. On a measured live store dead_at was set on
    3 of 9,998 rows and last_verified_at on 0.36%, because the existing
    machinery only ever ran against the judge pool of an interactive search.

    Reuses engine._classify_listing verbatim rather than reimplementing the
    check order. That function is documented as DB-free specifically so it can
    run on a worker thread, which is exactly this context, and its docstring
    carries the warning that matters: never call _EXPIRED_LISTING_RE raw, since
    a live bebee posting matches it on page furniture and dead_reason cannot be
    undone.

    ONE DELIBERATE DIVERGENCE from the in-run design: no browser escalation.
    engine._verify_via_browser launches a second Playwright instance, which is
    the wrong trade unattended on a box that is also serving searches -- and
    unlike the in-run path, nobody is waiting on the answer. An unverifiable row
    is simply recorded and retried next pass; across daily passes the ~22% shrug
    rate converges without it.

    Fail-open throughout: unverifiable is never treated as dead.
    """
    from concurrent.futures import ThreadPoolExecutor
    from . import engine as eng
    import full_auto as fa

    started = time.monotonic()
    cutoff = datetime.utcnow() - timedelta(days=OBSERVE_RECHECK_DAYS)

    # Oldest observation windows first: those are the rows whose death is most
    # informative to the age question, and the ones a stale verdict hurts most.
    rows = db.execute(
        select(ListingObservation)
        .where(ListingObservation.dead_at.is_(None))
        .where(ListingObservation.url.isnot(None))
        .where((ListingObservation.last_verified_at.is_(None))
               | (ListingObservation.last_verified_at < cutoff))
        .order_by(ListingObservation.first_seen.asc())
        .limit(limit)
    ).scalars().all()
    if not rows:
        return {"ok": True, "checked": 0, "reason": "nothing due for re-check"}

    # Adzuna's /jobs/land/ad/ interstitial answers every request with a stub, so
    # fetching it spends a request to learn nothing. Route those at the detail
    # page instead -- the same split engine._needs_liveness_check already makes.
    direct, adzuna = [], []
    for row in rows:
        (adzuna if eng._KNOWN_DEAD_END_URL_RE.search(row.url or "") else direct).append(row)

    def classify(row) -> tuple:
        job = {"url": row.url, "title": row.title}
        try:
            state, detail, jsonld = eng._classify_listing(fa, job)
        except Exception as e:  # a crawl must never take the process down
            return row, "unverifiable", type(e).__name__, {}
        return row, state, detail, jsonld

    results: list[tuple] = []
    if direct:
        with ThreadPoolExecutor(max_workers=OBSERVE_RECHECK_WORKERS) as ex:
            results.extend(ex.map(classify, direct))
    for row in adzuna:
        # fetch_adzuna_details resolves /details/{id}; absence of a description
        # there is the same "dead" signal the in-run enrichment path uses.
        try:
            text = fa.fetch_adzuna_details([row.url]) or {}
            results.append((row, "alive" if text else "unverifiable", "adzuna_detail", {}))
        except Exception as e:
            results.append((row, "unverifiable", type(e).__name__, {}))

    now = datetime.utcnow()
    counts = {"alive": 0, "dead": 0, "unverifiable": 0}
    dead_idents: list[str] = []
    for row, state, detail, _jsonld in results:
        counts[state] = counts.get(state, 0) + 1
        if dry_run:
            continue
        row.last_verified_at = now
        if state == "dead":
            # Stamped once, never overwritten -- same invariant as
            # engine._persist_dead_scrapes.
            if row.dead_at is None:
                row.dead_at = now
                row.dead_reason = detail
            dead_idents.append(row.identity_hash)

    if dry_run:
        for row, state, detail, _ in results[:limit]:
            print(f"[recheck:dry] {state:<13} {detail:<22} {(row.title or '')[:44]:<46} {row.url}")
        return {"ok": True, "dry_run": True, "checked": len(results), **counts}

    db.commit()

    # Fan out to the per-profile store so the pipeline's existing
    # `dead_reason IS NULL` selectors exclude these for free -- no new
    # suppression path -- and retire any card already sitting in an inbox.
    hidden = 0
    if dead_idents:
        for chunk in _chunks(dead_idents, 400):
            db.execute(
                update(JobSeen)
                .where(JobSeen.identity_hash.in_(chunk))
                .where(JobSeen.dead_reason.is_(None))
                .values(dead_reason="absent_on_recheck", dead_at=now, last_verified_at=now)
            )
        db.commit()
        affected = db.execute(
            select(JobSeen.profile_id).where(JobSeen.identity_hash.in_(dead_idents)).distinct()
        ).scalars().all()
        for pid in affected:
            hidden += eng._auto_hide_dead_roles(db, pid, dead_idents)
        db.commit()

    out = {"ok": True, "checked": len(results), **counts,
           "roles_auto_hidden": hidden,
           "elapsed_s": round(time.monotonic() - started, 1)}
    print(f"[recheck] {out}")
    return out


def observe_status(db: Session) -> dict:
    """Progress readout for GET /admin/observe.

    The number that actually matters is `distinct_observation_days`: a crawl
    that has silently stopped looks identical to a healthy one in every other
    field, and it is the one failure mode that costs time you cannot get back.
    """
    total = db.execute(select(func.count(ListingObservation.identity_hash))).scalar() or 0
    dead = db.execute(select(func.count(ListingObservation.identity_hash))
                      .where(ListingObservation.dead_at.isnot(None))).scalar() or 0
    verified = db.execute(select(func.count(ListingObservation.identity_hash))
                          .where(ListingObservation.last_verified_at.isnot(None))).scalar() or 0
    first = db.execute(select(func.min(ListingObservation.first_seen))).scalar()
    last = db.execute(select(func.max(ListingObservation.last_seen))).scalar()

    # Distinct observation days, and the best window any single listing has.
    days: set[int] = set()
    best = 0
    for (raw,) in db.execute(select(ListingObservation.seen_dates)
                             .where(ListingObservation.seen_dates.isnot(None))):
        parsed = {int(d) for d in (raw or "").split(",") if d.strip().isdigit()}
        days |= parsed
        best = max(best, len(parsed))

    store_age = (last - first).days if (first and last) else 0
    gate_days, evergreen_days = _observation_thresholds()
    return {
        "listings_tracked": total,
        "known_dead": dead,
        "directly_verified": verified,
        "first_seen": first.isoformat() if first else None,
        "last_seen": last.isoformat() if last else None,
        "store_age_days": store_age,
        "distinct_observation_days": len(days),
        "max_seen_days_on_one_listing": best,
        # What the longitudinal rules are still waiting for, so the readout
        # answers "is this working yet" without anyone doing the arithmetic.
        "observation_gate_days": gate_days,
        "observation_clock_open": store_age >= gate_days,
        "evergreen_needs_seen_days": evergreen_days,
    }


def _observation_thresholds() -> tuple[int, int]:
    """The observation gates, read from full_auto rather than redefined here.

    Two modules disagreeing about what "evergreen" means is exactly the failure
    SOFT_GATE_AXES exists to prevent, so these are never copied. Imported lazily
    and behind a try: full_auto pulls in crawl4ai/playwright, and importing it at
    module scope would mean the API could no longer boot without them -- the
    reason engine.py imports it lazily too. The fallback values are only ever
    used for a status readout, never for a decision."""
    try:
        import full_auto as fa
        return int(fa.OBSERVATION_MIN_STORE_DAYS), int(fa.EVERGREEN_SEEN_DAYS)
    except Exception:
        return 30, 30
