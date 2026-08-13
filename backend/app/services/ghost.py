"""Ghost-listing detection: named rules over facts we already hold.

A ghost listing is an advert with no real vacancy behind it -- a role already
filled, a pipeline ad collecting CVs, a req that was cancelled but never taken
down. The candidate cannot tell from the page, and it costs them an application.

WHY A COUNT OF NAMED RULES AND NOT A WEIGHTED SCORE
There is nothing to fit weights to. The app has 24 feedback rows and, at the
time of writing, zero terminal application outcomes ever recorded -- a tuned
weight vector on that data is an aesthetic choice dressed as an empirical one.
This codebase has already made the same call twice for the same reason:
dynamic_hard_drop_threshold COUNTS failed soft axes rather than scoring them,
and FINAL_EVAL v16 rewrote fit_level away from an overall impression toward a
mechanical derivation off a named checklist -- because an opaque grade let the
model cite a decisive concern and still return "strong".

There is a real cost: a count cannot say that 400 days is worse than 95. That is
handled the way screen_gate handles it -- a few DECISIVE rules that are
individually sufficient, alongside ORDINARY ones that must agree in pairs. Two
tiers, no continuous scale, and every level is explainable as the rules that
produced it.

THE OTHER HALF OF WHY: a chip reading "Possible ghost listing" is an accusation
about a named employer. "0.71" cannot be shown to anyone; "posted 8 months ago,
and the text says it is always accepting applications" can. The rules ARE the
explanation, and a score discards it at the moment of computation.

INVARIANTS
  * Every rule fires on POSITIVE evidence. None fires on missing data. A listing
    with no posted_at produces no signal -- the same discipline
    full_auto._listing_age_tag follows ("silence is not evidence of age"). This
    is what lets the card treat an absent badge as "nothing fired" rather than
    "unknown", and what makes the /search "none flagged" line honest. Do not add
    a rule that fires on absence without changing both of those.
  * No LLM, no network, no DB. Every rule is a fact checkable for free -- the
    bar engine._heuristic_prescreen and _pool_quality_prescreen already set.
  * Never a hard drop. Same posture as _listing_age_tag: a downgrade, never a
    filter. A months-old listing is still applicable-to, and unlike dead_reason
    a ghost suspicion must be undoable.
  * Thresholds that already exist in full_auto are IMPORTED, never restated. Two
    modules disagreeing about what "evergreen" means is exactly the failure
    SOFT_GATE_AXES exists to prevent.

PHASING
The four snapshot rules work today. The four longitudinal ones (marked DORMANT)
are written and wired but cannot fire until the observation crawl has run daily
for long enough -- they are gated on data that does not exist yet, so shipping
them dormant is safe and avoids a second pass through this file later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# ── Thresholds ────────────────────────────────────────────────────────────────
# A definite posting date this old is worth a signal. Deliberately well above
# the candidate's own "maximum listing age" preference (default 30 days): that
# preference is about freshness, this is about whether a vacancy still exists.
# 90 days is roughly twice a normal UK time-to-hire.
GHOST_STALE_DAYS = 90
# ...and past a year, one signal is enough on its own. An advert running this
# long is not a slow hire.
GHOST_ANCIENT_DAYS = 365
# For a merely APPROXIMATE date (an ATS updated_at aliased into posted_at, see
# JobSeen.posted_at_approx), the bar is higher and it is never decisive: the
# board never claimed this was the posting date, so it is weaker evidence.
GHOST_UNTOUCHED_DAYS = 180
# A repost family must span this long before it reads as re-advertising rather
# than one recruiter posting the same ad across several towns on one day.
GHOST_REPOST_SPAN_DAYS = 45
GHOST_REPOST_MIN_ROWS = 3

# A stated start date must be MORE than this many days past before it counts.
# Employers genuinely run a day or two late on a target start, and this also
# absorbs the slop in reading a date out of prose (timezones, a year we had to
# infer). Set at 2 rather than higher because the live case that motivated the
# rule -- a four-week temporary contract whose stated start had gone -- was
# three days past when it was shown, and that is exactly the listing a candidate
# should not spend an application on.
GHOST_START_DATE_GRACE_DAYS = 2
# ...and past a month, the advert is plainly not being maintained. Decisive, on
# the same reasoning as stated_age_absurd: a start date is the employer's own
# statement about when the work begins, and a month past it the job has either
# started without the reader or was never filled and the ad left standing.
GHOST_START_DATE_LONG_DAYS = 30
# How far BEFORE the advert a start date with no stated year may fall before the
# year is read as the next one instead. Not zero: adverts are routinely written
# a few days after the start they quote ("start date: Monday 10th August",
# posted the 12th), and reading that as next August would silence the rule on
# precisely the listings it exists to catch.
GHOST_START_DATE_BACKDATE_SLACK = 45

# Language that announces the ad is a standing pipeline rather than one vacancy.
# High precision precisely BECAUSE honest evergreen ads say so -- the oldest row
# in the live store is a 2018 Personio posting titled "General Application for
# Can-do Engineers and/or Visionary Builders", which is not deceptive, just not
# a vacancy.
#
# TWO PATTERNS, AND THE SPLIT IS THE WHOLE RULE. A first cut ran one broad
# pattern over title+body and over-fired badly: 22 of its 57 hits were a single
# staffing agency (blue-united-sourcing) whose boilerplate says "Talent Network"
# at the top of EVERY posting, including "Registered Nurse (RN) - ER" and
# "Optometrist - Community Hospital" -- specific, real, immediately-fillable
# vacancies. Another 4 were "we are always looking" on an agency's "Operations
# Manager" and "Outside Sales Representative".
#
# The distinction those cases teach: a pipeline ad announces itself in its
# TITLE. A talent-network or always-hiring phrase in the BODY is a
# call-to-action appended to a real posting, and is evidence of nothing.
#
# So the title pattern is broad, and the body pattern carries only phrases that
# can only be describing THIS listing -- you cannot append "this is a general
# application" to a specific vacancy without contradicting yourself, whereas you
# can append "join our talent network" to anything.
_PIPELINE_TITLE_RE = re.compile(
    r"\b(?:"
    r"talent\s+(?:pool|community|network|pipeline|hub)"
    r"|general\s+(?:application|interest)"
    r"|speculative\s+application"
    r"|open\s+application"
    r"|expression\s+of\s+interest"
    r"|register\s+your\s+interest"
    r"|future\s+opportunit"
    r"|general\s+enquir"
    r"|pipeline\s+(?:role|req|requisition)"
    r")",
    re.I,
)
_PIPELINE_BODY_RE = re.compile(
    r"\b(?:"
    r"always\s+(?:accepting|welcoming)\s+applications"
    r"|this\s+is\s+(?:a\s+)?(?:general|speculative|open)\s+application"
    r"|no\s+specific\s+(?:role|vacancy|position)\s+in\s+mind"
    r"|not\s+(?:a\s+)?(?:live|current|specific)\s+vacancy"
    r"|we\s+are\s+not\s+currently\s+(?:hiring|recruiting)\s+for"
    r")",
    re.I,
)
# Only the head of the body is consulted. A listing that really is a pipeline
# says so up front; the same words further down are page furniture.
_PIPELINE_SCAN_CHARS = 1500

# ── Stated start date ─────────────────────────────────────────────────────────
# The employer's own statement about when the work begins, which -- unlike every
# other rule here -- is evidence about THIS vacancy rather than about the advert.
# Once that day has gone, either someone is already doing the job or nobody has
# touched the listing since. Both are exactly what this module is for, and the
# case that motivated the rule was invisible to every existing one: a four-week
# temporary contract, posted eight days earlier (so far too fresh for any age
# rule), stating "Start Date : Monday 10th August" -- three days before it was
# shown. The expensive judge noticed and wrote "the stated start date has
# already passed, so confirm that the vacancy is still active" into its
# concerns, which is a footnote on a card that carried no risk flag at all.
#
# MEASURED against the live store (16,681 text-bearing rows) before shipping,
# which is the bar a free rule has to meet here -- the risk is a silent false
# positive, so what matters is the misses, not the hit count. Two false-positive
# classes came out of round one and both are designed out below:
#
#   * MONTH-ONLY dates. "Start Date: September 2026" matched with the YEAR's
#     leading "20" read as the day of the month -- 8 of 50 hits. Month-only is
#     also genuinely too coarse to call passed (a September start is not late
#     until October), so a day of the month is REQUIRED and month-only never
#     fires. `\d{1,2}(?!\d)` is what stops a year being read as a day.
#   * "commencing". In UK job adverts "week commencing" is the ordinary phrase
#     for "week beginning", and in this corpus it introduced INTERVIEW dates:
#     "Proposed interview date: Week Commencing 7th September 2026". The label
#     set therefore names the start explicitly and never matches that idiom.
#
# KNOWN LIMIT, and it is the positive-evidence invariant doing its job rather
# than a bug: _annotate_ghost runs at pool admission, so on the run that first
# DISCOVERS a listing this reads whatever text was in the store -- for Reed and
# Adzuna a ~455-char opening teaser, and a start date almost always sits below
# that in the details block. So the rule is typically silent on a listing's
# first run and fires from the next one, once enrichment or a scrape has
# persisted the fuller text. Silence is the correct answer there: we genuinely
# have not seen a start date. Do not move this later in the pipeline to "fix"
# it -- _selection_score reads GHOST_SELECTION_PENALTY during gate/rank, so the
# assessment has to exist by then.
_MONTH_RE = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun[e]?|jul[y]?|"
             r"aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_START_LABEL = r"(?:start(?:ing)?\s*(?:date|day)|date\s+of\s+start|start(?:s|ing)?\s+on)"
_DAY = r"\d{1,2}(?!\d)"
_YEAR = r"20\d{2}"
_START_DATE_RE = re.compile(
    rf"{_START_LABEL}\s*[:\-–]?\s*"
    # An optional weekday name ("Monday 10th August"), which is how the case
    # that motivated this rule was written.
    rf"(?:[a-z]{{3,9}}day,?\s+)?"
    rf"(?:"
    rf"(?P<n1>\d{{1,2}})[/-](?P<n2>\d{{1,2}})[/-](?P<ny>20\d{{2}}|\d{{2}})(?!\d)"
    rf"|(?P<d1>{_DAY})(?:st|nd|rd|th)?\s+(?P<m1>{_MONTH_RE})\.?(?:\s+(?P<y1>{_YEAR}))?"
    rf"|(?P<m2>{_MONTH_RE})\.?\s+(?P<d2>{_DAY})(?:st|nd|rd|th)?(?:,?\s+(?P<y2>{_YEAR}))?"
    rf")",
    re.I,
)

# An employer who says the start is negotiable has told you the date is not a
# deadline, so a passed one says nothing. Measured: this is what separates
# "Preferred Start Date: Flexible (Target start date - March 23, 2026)" -- a
# real listing in the store -- from a hard date nobody updated.
#
# `flexible` needs the negative lookahead and DISTANCE CANNOT REPLACE IT. In the
# real listing above, "Flexible" sits 22 characters from the date; in "We offer
# flexible working. Start Date: 1st July 2026" the boilerplate sits 18 away. Any
# window wide enough to catch the first also catches the second and silences the
# rule on an ordinary advert, so the two are told apart by what the word is
# attached to, not by how near it is.
_FLEXIBLE_START_RE = re.compile(
    r"\b(?:"
    r"flexible(?!\s+(?:work|hours|hour|schedul|benefit|arrangement|approach|"
    r"holiday|leave|start\s+and\s+finish))"
    r"|negotiable|asap|immediate(?:ly)?|to\s+be\s+confirmed|tbc|tbd"
    r"|on\s+or\s+around|approximate(?:ly)?|rolling|ongoing"
    r")\b", re.I)
# How far either side of the label to look for that wording. Small on purpose:
# these are common words in job adverts and a wide window would suppress the
# rule everywhere.
_FLEXIBLE_SCAN_CHARS = 45

# ── Agency detection ──────────────────────────────────────────────────────────
# Agency reposting is routine business, not ghosting, so the repost-family rules
# are suppressed for one. The age rules are NOT suppressed: an ad standing for a
# year with no vacancy behind it is the candidate's problem whoever posted it.
#
# Measured on the live store, the obvious name test is far too weak to stand
# alone -- it caught 271 of 1,547 distinct companies but only 5.9% of ROWS, and
# missed Noir (108 rows), Hays, Robert Half, Michael Page, Adecco and Randstad
# outright. Hence three tests, any of which is sufficient.
_AGENCY_NAME_RE = re.compile(
    r"\b(?:recruit\w*|resourcing|staffing|talent|selection|personnel|"
    r"associates|consultan\w*|manpower|appointments|headhunt\w*)\b", re.I)

# The measured misses. Same posture as direct_employer._FOREIGN_ATS_HOSTS: where
# a rule cannot be made to generalise, a list observed from real data beats a
# cleverer rule that still gets the common cases wrong.
_KNOWN_AGENCIES = frozenset({
    "noir", "hays", "adecco", "randstad", "michael page", "robert half", "reed",
    "lorien", "sanderson", "pontoon", "searchability", "harnham", "opus",
    "oscar technology", "robert walters", "mcs group", "proactive appointments",
    "rise technical", "spectrum it", "penguin", "gleeson", "elevation",
    "method resourcing", "yolk", "plum personnel", "teksystems", "sthree",
    "nigel frank", "understanding solutions", "in technology group",
})

# Structural tell, computed once per run over aggregator-sourced rows only.
# The shape is FEW TITLES, MANY LOCATIONS -- one ad sprayed across towns.
#
# It is emphatically NOT "posts across many unrelated role families": measured,
# that ranking is dominated by ATS crawl artefacts (gh:spacex is 1,943 rows and
# 664 distinct title tokens -- a real employer whose whole board was enumerated).
#     noir    107 rows  16 titles  86 locations  ratio 5.4
#     wise     24 rows  24 titles   1 location   ratio 0.04
AGENCY_MIN_ROWS = 8
AGENCY_LOC_TITLE_RATIO = 2.0
# WHAT THIS HALF ACTUALLY DETECTS, and why its one "false positive" is fine.
#
# Measured on the live store it fires on exactly two companies: `noir` (already
# caught by name) and `howdens joinery` -- 15 rows, 4 titles, 15 locations, a
# genuine multi-site retailer hiring the same depot role across 15 towns, not an
# agency at all. On the face of it that is a 50%-precision test contributing no
# true positives, and the obvious move is to delete it.
#
# It should stay, because the label is what is wrong, not the test. The property
# it measures is "posts ONE role across MANY locations", and that -- not
# employer type -- is precisely the property the repost rules need to know
# about: a retailer advertising one depot job in 15 towns produces the identical
# signature to an agency spraying geographic variants, and neither is
# re-advertising a vacancy that keeps not being filled. Suppressing repost_burst
# for Howdens is the CORRECT outcome, reached through a slightly wrong name.
#
# This is only safe because of two constraints that must hold:
#   * is_agency() may only ever MODIFY which ghost rules apply, and is never
#     itself ghost evidence;
#   * nothing agency-derived reaches the card. A national care provider or
#     logistics firm has the same shape, and calling one an agency on screen
#     would be a visible error rather than an internal approximation.

# ── Rule tiers ────────────────────────────────────────────────────────────────
# DECISIVE rules are individually sufficient for "high". ORDINARY rules must
# agree in pairs. Mirrors screen_gate's hard-vs-soft axis split.
DECISIVE = frozenset({"stated_age_absurd", "pipeline_language", "takedown_repost",
                      "start_date_long_passed"})

# Human-readable, second-person, and phrased as facts about the LISTING rather
# than accusations about the employer -- a listing can be stale for entirely
# innocent reasons and the card must not assert motive. Rendered under the
# card's collapsible analysis via engine._compose_analysis' §ghost marker.
SIGNAL_TEXT = {
    "stated_age_extreme":
        "This advert has been running for over three months.",
    "stated_age_absurd":
        "This advert has been running for over a year.",
    "stale_untouched":
        "The board has not updated this listing in over six months.",
    "pipeline_language":
        "The text describes an open talent pool or speculative application "
        "rather than one specific vacancy.",
    # Phrased as the listing's own claim, and hedged, because the date was read
    # out of prose: the reader can check it against the advert in one glance,
    # which is the whole reason these are sentences rather than a score.
    "start_date_passed":
        "The start date the listing gives has already passed.",
    "start_date_long_passed":
        "The start date the listing gives passed over a month ago.",
    "date_refreshed":
        "Its posting date has been refreshed while we have been seeing the same "
        "advert unchanged.",
    "evergreen_observed":
        "It has been advertised near-continuously since we first saw it, which "
        "fits a standing advert rather than a single vacancy.",
    "repost_burst":
        "The same role has been re-advertised repeatedly over a long period.",
    "takedown_repost":
        "An identical advert was taken down and then posted again.",
}


@dataclass
class GhostContext:
    """Per-run state the rules read. Built once, before any rule runs.

    store_age_days gates every observation-clock rule, for the reason
    full_auto.OBSERVATION_MIN_STORE_DAYS exists: a fresh or reset store makes
    every row look newly-discovered, and no per-row threshold can tell that
    apart from a genuinely new listing.
    """
    now: datetime = field(default_factory=datetime.utcnow)
    store_age_days: float = 0.0
    # repost_key -> {"rows": int, "span_days": int, "first_seen_days": int,
    #                "dead_before": datetime | None}
    repost_groups: dict = field(default_factory=dict)
    agencies: frozenset = frozenset()
    # Thresholds imported from full_auto so this module cannot drift from the
    # code that already applies them elsewhere.
    observation_min_store_days: int = 30
    evergreen_seen_days: int = 30
    evergreen_seen_density: float = 0.6

    @property
    def observation_clock_open(self) -> bool:
        return self.store_age_days >= self.observation_min_store_days


def _norm_company(name: str) -> str:
    from .engine import _norm_company as impl
    return impl(name or "")


def is_agency(company: str, ctx: GhostContext | None = None) -> bool | None:
    """Whether this listing's `company` names a recruitment agency.

    Three-state: None when there is no company to judge (115 rows in the live
    store are blank), because "unknown" and "not an agency" lead to different
    handling and collapsing them would silently apply the repost rules to
    aggregator rows we cannot attribute.
    """
    name = (company or "").strip()
    if not name:
        return None
    norm = _norm_company(name)
    if norm in _KNOWN_AGENCIES:
        return True
    if _AGENCY_NAME_RE.search(name):
        return True
    if ctx and norm in ctx.agencies:
        return True
    return False


def is_known_agency(company: str) -> bool:
    """Whether `company` is one of the NAMED agencies on _KNOWN_AGENCIES.

    Deliberately the narrowest of is_agency's three tests, and the only one that
    amounts to POSITIVE IDENTIFICATION of a real, established firm with a public
    footprint. The other two must never be substituted here:

    * _AGENCY_NAME_RE is self-declared. "Recruitment"/"Talent"/"Resourcing" in a
      name is exactly what a CV-farming operation calls itself, so treating that
      as evidence of legitimacy inverts the signal it is being asked about.
    * the structural test (ctx.agencies) measures a POSTING PATTERN -- few
      titles across many locations -- which says nothing about whether anyone
      real is behind it, and whose one measured unique hit is a retailer.

    Used by engine's scam-verify to clear a judge-raised `scam_suspect` flag:
    the judge's softer signals (an anonymised "one of our clients" framing, a
    content-free company blurb, a high same-run posting volume) are the NORMAL
    working traits of an agency, which is why the judge's own prompt already
    carves out listings arriving through a registered ATS account for the same
    reason. This is the same exception reached by a different route, for an
    agency we can name rather than one whose ATS account we happened to see.

    Prefix-matched on a leading token so the trading name reaches its full legal
    one -- "Hays Specialist Recruitment Limited" is the same Hays, and the
    exact-match form in is_agency only ever caught it via the name regex above,
    which is unavailable here. Anchored at the START and required to end on a
    word boundary, so this can never fire on a name that merely contains one of
    these words further in.
    """
    norm = _norm_company(company or "")
    if not norm:
        return False
    if norm in _KNOWN_AGENCIES:
        return True
    return any(norm.startswith(a + " ") for a in _KNOWN_AGENCIES)


def _is_pipeline_ad(job: dict) -> bool:
    """Whether the listing is a standing pipeline ad rather than one vacancy.

    Title and body are tested with DIFFERENT patterns and must stay that way --
    see the note above _PIPELINE_TITLE_RE for the measurement that forced it.
    Collapsing them back into one broad pattern over title+body re-flags a
    staffing agency's entire genuine caseload."""
    if _PIPELINE_TITLE_RE.search(job.get("title") or ""):
        return True
    body = job.get("full_text") or job.get("_full_text") or job.get("snippet") or ""
    return bool(_PIPELINE_BODY_RE.search(body[:_PIPELINE_SCAN_CHARS]))


_MONTH_NUM = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _as_date(value) -> date | None:
    """Best-effort date out of whatever a job dict is carrying (datetime, ISO
    string, or nothing). Returns None rather than raising -- an unreadable
    anchor must make the rule silent, never make it guess."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "")).date()
        except ValueError:
            return None
    return None


def _candidate_start_dates(m: re.Match, anchor: date) -> list[date]:
    """Every reading of the matched date that is consistent with the listing.

    Usually one. Two things force a list:

    * A NUMERIC date is ambiguous between UK D/M/Y and US M/D/Y whenever both
      numbers are 12 or under, and the store holds both conventions (a UK
      "01/08/26" and a US "2/23/2026" sit a few rows apart). Guessing a
      convention would be wrong half the time for one of them, so both readings
      are returned and the caller only fires if they AGREE the date has passed.
    * A date with NO YEAR ("Monday 10th August") has to have one inferred. The
      anchor -- the posting date, or failing that when we first saw the listing
      -- is what does it: a start date sits on or after the advert, so the
      earliest year that puts it within GHOST_START_DATE_BACKDATE_SLACK of the
      anchor is the intended one. Without this, every August date on a January
      advert would read as seven months passed.
    """
    out: list[date] = []

    def add(y: int | None, mo: int, d: int) -> None:
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return
        if y is not None:
            try:
                out.append(date(y, mo, d))
            except ValueError:
                pass
            return
        # No year stated: earliest candidate not implausibly before the advert.
        for cand_year in (anchor.year - 1, anchor.year, anchor.year + 1):
            try:
                cand = date(cand_year, mo, d)
            except ValueError:
                continue
            if cand >= anchor - timedelta(days=GHOST_START_DATE_BACKDATE_SLACK):
                out.append(cand)
                return

    if m.group("n1"):
        raw_year = m.group("ny")
        year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
        a, b = int(m.group("n1")), int(m.group("n2"))
        add(year, b, a)          # D/M/Y
        if a != b:
            add(year, a, b)      # M/D/Y
    elif m.group("d1"):
        year = int(m.group("y1")) if m.group("y1") else None
        add(year, _MONTH_NUM[m.group("m1")[:3].lower()], int(m.group("d1")))
    else:
        year = int(m.group("y2")) if m.group("y2") else None
        add(year, _MONTH_NUM[m.group("m2")[:3].lower()], int(m.group("d2")))
    return out


def _passed_start_date(job: dict, now: datetime) -> int | None:
    """Days since the listing's own stated start date, or None.

    None is the answer for the overwhelming majority of listings and means "no
    signal" -- no stated date, no readable date, a date still to come, or a date
    the listing itself called flexible. Never "unknown, so assume the worst":
    see this module's positive-evidence invariant.
    """
    text = job.get("full_text") or job.get("_full_text") or job.get("snippet") or ""
    if not text:
        return None
    m = _START_DATE_RE.search(text)
    if not m:
        return None

    # An employer who called the start flexible has said the date is a target,
    # not a deadline. Scanned around the LABEL, not the whole listing.
    window = text[max(0, m.start() - _FLEXIBLE_SCAN_CHARS):m.end() + _FLEXIBLE_SCAN_CHARS]
    if _FLEXIBLE_START_RE.search(window):
        return None

    anchor = _as_date(job.get("_posted_at")) or _as_date(job.get("_first_seen"))
    if anchor is None:
        return None
    candidates = _candidate_start_dates(m, anchor)
    if not candidates:
        return None
    # Every surviving reading must agree the date has passed, so an ambiguous
    # numeric date can only fire when the ambiguity does not matter.
    days = [(now.date() - c).days for c in candidates]
    return min(days) if min(days) > GHOST_START_DATE_GRACE_DAYS else None


def evaluate(job: dict, ctx: GhostContext) -> tuple[str | None, list[str]]:
    """(level, signals) for one candidate. level is "high" | "medium" | None.

    Pure: no DB, no network, no LLM, no mutation of `job`.
    """
    import full_auto as fa

    signals: list[str] = []

    # ── Age, from the board's own DEFINITE claim ──────────────────────────────
    # _listing_definite_age_days, never job["_posted_at"] directly. It returns
    # None for posted_at_approx, and that distinction is load-bearing: 528 rows
    # in the live store carry an approximate date (gh:tegnainc 175,
    # gh:doordashusa 134), which is an ATS updated_at, not a posting date.
    # Reading those as "posted" would make Greenhouse boards the single largest
    # ghost population in the store with every hit a false positive.
    definite_age = fa._listing_definite_age_days(job, ctx.now)
    if definite_age is not None and definite_age >= GHOST_ANCIENT_DAYS:
        signals.append("stated_age_absurd")
    elif definite_age is not None and definite_age >= GHOST_STALE_DAYS:
        signals.append("stated_age_extreme")

    # The approximate date gets its own, higher bar and never becomes decisive.
    if job.get("_posted_at_approx"):
        approx_age = fa._days_since(job.get("_posted_at"), ctx.now)
        if approx_age is not None and approx_age >= GHOST_UNTOUCHED_DAYS:
            signals.append("stale_untouched")

    # ── The listing's own words ───────────────────────────────────────────────
    if _is_pipeline_ad(job):
        signals.append("pipeline_language")

    # The one rule that reads a date about the VACANCY rather than the advert.
    # Two steps for the reason the age pair has two: a count cannot say that
    # forty days past is worse than three, so the split says it instead.
    start_days = _passed_start_date(job, ctx.now)
    if start_days is not None:
        signals.append("start_date_long_passed"
                       if start_days >= GHOST_START_DATE_LONG_DAYS
                       else "start_date_passed")

    signals += _longitudinal_signals(job, ctx)

    decisive = sum(1 for s in signals if s in DECISIVE)
    ordinary = len(signals) - decisive
    if decisive >= 1 or ordinary >= 2:
        level = "high"
    elif ordinary == 1:
        level = "medium"
    else:
        level = None
    return level, signals


def _longitudinal_signals(job: dict, ctx: GhostContext) -> list[str]:
    """DORMANT until the observation crawl has run daily for long enough.

    Every rule here is gated on data that does not exist in a young store, so
    they cannot fire early -- which is why they ship now rather than in a second
    pass through this file. See services/observe.py for what feeds them, and
    note the gating is on OUR observation clock, never on the board's claims.
    """
    if not ctx.observation_clock_open:
        return []

    import full_auto as fa

    out: list[str] = []
    observed_days = fa._days_since(job.get("_first_seen"), ctx.now)
    if observed_days is None:
        return out
    seen_days = job.get("_seen_days") or 1
    posted_days = fa._days_since(job.get("_posted_at"), ctx.now)

    # Date laundering: a fresh claimed posting date sitting on top of a long
    # observation window means the date was refreshed, not that the role is new.
    # Same +14 tolerance _listing_age_tag uses, so the two agree.
    if (posted_days is not None and observed_days >= fa.OBSERVED_MENTION_DAYS
            and posted_days + 14 < observed_days):
        out.append("date_refreshed")

    # Standing/pipeline ad: advertised on most days since discovery. Thresholds
    # imported, not restated.
    density = seen_days / max(1, observed_days)
    if seen_days >= ctx.evergreen_seen_days and density >= ctx.evergreen_seen_density:
        out.append("evergreen_observed")

    group = ctx.repost_groups.get(job.get("_repost_key") or "")
    if group:
        # A takedown followed by a fresh posting of the same company+title is
        # the strongest available evidence, because a filled vacancy does not
        # come back. Decisive.
        dead_before = group.get("dead_before")
        first_seen = job.get("_first_seen")
        if dead_before and first_seen:
            fs = first_seen if isinstance(first_seen, datetime) else fa._loose_date_to_iso(first_seen)
            if isinstance(fs, str):
                try:
                    fs = datetime.fromisoformat(fs)
                except ValueError:
                    fs = None
            if fs and fs > dead_before:
                out.append("takedown_repost")

        # Repeated re-advertising over a long span. All three guards are
        # load-bearing: without them this fires 88 times on a single agency.
        # `noir|.net developer` is 88 rows / 88 distinct URLs / 6 distinct
        # posted_at values / all inside 3 days -- one recruiter spraying
        # geographic variants simultaneously, which is not reposting at all.
        if (group.get("rows", 0) >= GHOST_REPOST_MIN_ROWS
                and group.get("span_days", 0) >= GHOST_REPOST_SPAN_DAYS
                and group.get("first_seen_days", 0) >= 2
                and not job.get("_is_agency")):
            out.append("repost_burst")
    return out


def describe(signals: list[str] | None) -> list[str]:
    """Fired signals as sentences for the card. Unknown slugs are dropped rather
    than rendered raw -- a stored verdict from an older rule set must degrade to
    fewer lines, never to a slug leaking onto the page."""
    return [SIGNAL_TEXT[s] for s in (signals or []) if s in SIGNAL_TEXT]
