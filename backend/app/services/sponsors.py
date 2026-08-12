"""Match a job listing's company name against the UK licensed-sponsor register.

Pure, offline, no API and no LLM: every lookup is a dict/set hit against the
generated `backend/app/uk_sponsor_gen.py` (see scripts/gen_uk_sponsors.py), the
same dev-time-generator / committed-plain-data-module posture as uk_geo_gen and
uk_charity_gen.

WHAT THIS CAN AND CANNOT TELL YOU
The Home Office register names ORGANISATIONS, with no domain, no companies-house
number and no sector. A job listing carries a free-text company name written by
whoever posted it. So this is name matching, and the honest measured numbers on
a live 9,042-row store (1,244 unique companies) are:

    exact normalised match      256 unique companies (20.6%)
    + guarded prefix match      296 unique companies (23.8%)   892/8,931 rows (10.0%)

Two limitations are structural and no amount of matcher tuning fixes them:

  * RECRUITMENT AGENCIES. The listing names the agency, not the employer who
    would hold the licence. "Hays Specialist Recruitment Limited" does not match
    even though "Hays PLC" is on the register, and an agency that IS on the
    register tells you nothing about whether the end employer can sponsor.
  * BLANK COMPANIES. Careerjet and Google-organic rows routinely arrive with no
    company at all (111 rows in that store). There is nothing to match.

Callers decide what to do with a miss. `engine._filter_by_sponsor` treats it as
a drop, which is deliberate and strict -- a candidate who needs sponsorship is
better served by a short list of confirmed sponsors than a long list of maybes.

WHY THE PREFIX MATCH IS GUARDED
Register entries carry legal-entity tails a job board never writes ("Muller UK &
Ireland Group LLP T/A Muller Milk & Ingredients" vs a listing's "Muller UK &
Ireland"), so exact matching alone leaves real sponsors on the table. But
unguarded prefix matching is worse than useless: a company literally named
"Futures" would match "Wild Futures". Requiring at least TWO shared leading
tokens is the control. It costs genuine hits -- a listing that says just
"Kaplan" will not reach "Kaplan Financial Limited" -- and that is the correct
trade when the answer feeds a hard filter.
"""
from __future__ import annotations

import re
from functools import lru_cache

# Legal-form and geographic noise that appears on one side of a match and not
# the other. Dropped from BOTH sides, so "Claranet Limited" and "Claranet" agree.
# Kept deliberately short: every token removed here is a token that can no
# longer distinguish two different organisations.
_NOISE = {
    "limited", "ltd", "llp", "lp", "plc", "inc", "incorporated", "corp",
    "corporation", "company", "co", "group", "holdings", "holding",
    "uk", "gb", "international", "intl", "the",
}

# Trading-as and parent-brand separators. A register entry frequently carries
# both the legal entity and the trading name ("CLFIS (UK ) Ltd - Canada Life"),
# and the listing will only ever use one of them -- so both sides are expanded
# into variants and any variant matching is a match.
_ALIAS_SPLIT = re.compile(r"\s+t/?a\s+|\s+trading\s+as\s+|\s+-\s+|\s*\(", re.I)

_PUNCT = re.compile(r"[^a-z0-9 ]+")

# Below this many shared leading tokens a prefix match is not evidence. See the
# module docstring -- this is the false-positive control, not a tuning knob.
_MIN_PREFIX_TOKENS = 2


def normalise(name: str) -> tuple[str, ...]:
    """A company name -> its comparable token tuple ("" tokens dropped)."""
    text = (name or "").lower().replace("&", " and ")
    text = _PUNCT.sub(" ", text)
    return tuple(w for w in text.split() if w and w not in _NOISE)


def variants(name: str) -> list[tuple[str, ...]]:
    """The whole name plus each trading-as / parent-brand alias inside it.

    Order is whole-name-first so an exact hit on the full name is found before
    an alias, but every caller treats all variants equally."""
    out: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for part in [name or ""] + [p for p in _ALIAS_SPLIT.split(name or "") if p and p.strip()]:
        tokens = normalise(part)
        if tokens and tokens not in seen:
            seen.add(tokens)
            out.append(tokens)
    return out


def key(tokens: tuple[str, ...]) -> str:
    """The storage form of a token tuple (what uk_sponsor_gen holds, one/line)."""
    return " ".join(tokens)


# ── Index, built lazily ──────────────────────────────────────────────────────
# Deliberately not built at import: uk_sponsor_gen is a ~2MB module and the
# by-first-token index is ~127k splits, and a profile with the preference off
# must not pay either. Same posture as direct_employer's lazy full_auto import.
_EXACT: frozenset[str] | None = None
_BY_FIRST: dict[str, tuple[tuple[str, ...], ...]] | None = None


def _index() -> tuple[frozenset[str], dict[str, tuple[tuple[str, ...], ...]]]:
    global _EXACT, _BY_FIRST
    if _EXACT is None or _BY_FIRST is None:
        from app import uk_sponsor_gen

        keys = uk_sponsor_gen.sponsor_keys()
        by_first: dict[str, list[tuple[str, ...]]] = {}
        for k in keys:
            tokens = tuple(k.split(" "))
            by_first.setdefault(tokens[0], []).append(tokens)
        _EXACT = frozenset(keys)
        _BY_FIRST = {first: tuple(v) for first, v in by_first.items()}
    return _EXACT, _BY_FIRST


def sponsor_count() -> int:
    """How many distinct organisation keys the register yielded."""
    exact, _ = _index()
    return len(exact)


@lru_cache(maxsize=8192)
def match(company: str) -> str | None:
    """"exact" | "prefix" | None. Cached -- one run re-asks the same names often."""
    if not (company or "").strip():
        return None
    vs = variants(company)
    if not vs:
        return None
    exact, by_first = _index()

    for tokens in vs:
        if key(tokens) in exact:
            return "exact"

    for tokens in vs:
        if len(tokens) < _MIN_PREFIX_TOKENS:
            # A single-token company can only ever share ONE leading token, so
            # it can never clear the guard. Skipping it here is what keeps
            # "Futures" from reaching the "Wild Futures" bucket at all.
            continue
        for candidate in by_first.get(tokens[0], ()):
            n = min(len(tokens), len(candidate))
            if n >= _MIN_PREFIX_TOKENS and tokens[:n] == candidate[:n]:
                return "prefix"
    return None


def is_sponsor(company: str) -> bool:
    """Whether this company name resolves to a licensed sponsor.

    False means "not confirmed", which covers both "genuinely not on the
    register" and "the listing did not name the employer well enough to tell"
    (agency postings, blank companies). Callers that hard-drop on False are
    accepting that conflation -- see the module docstring."""
    return match(company) is not None


# ── What the LISTING ITSELF says ─────────────────────────────────────────────
# The register answers "does this EMPLOYER hold a licence". It cannot answer
# "will this VACANCY be sponsored", which is the question the candidate actually
# has -- a licensed employer routinely advertises roles it won't sponsor, and a
# role advertised by an agency (which the register can never resolve) may well
# be sponsored by the end employer. The listing's own words are the only source
# that speaks to the specific vacancy, and they are free to read.
#
# Free, offline and deterministic on purpose, not an LLM axis. These phrasings
# are highly formulaic, a regex is auditable and re-measurable against the
# store, and adding an axis to screen_gate would cost a `screen_v` bump -- i.e.
# re-screening the whole store to get identical answers for the untouched
# majority. It also reads the WHOLE text, where screen_gate sees only the first
# GATE_LISTING_TEXT_CHARS; the sponsorship line is almost always near the end
# ("Please note, we are unable to provide sponsorship...").
#
# MEASURED ON THE LIVE STORE (12,273 rows carrying text): 293 mention "sponsor"
# at all, and the great majority of those are NOT about visas. That ratio is the
# whole design problem, so the non-visa senses are excluded explicitly rather
# than hoped away:
#     "company-sponsored lunches / life insurance / activities / disability"
#     "executive sponsors", "program sponsors", "with strong senior sponsorship"
#     "drive event sponsorship sales", "connect with sponsorship prospects"
#     "sponsor innovation with purpose", "paired with a sponsor who mentors you"
#     "we sponsor co-working space in your city"
#     "manage visa applications ... needed for sponsoring employees"  <- a DUTY
# That last one is why a bare "visa" nearby is not sufficient context on its own
# for a positive: an immigration-team JD talks about sponsoring people all day.
_SPONSOR_SENSE_EXCLUDE = re.compile(
    r"company[- ]sponsored|"
    r"(?:executive|program(?:me)?|project|business|senior|corporate|board|clinical|"
    r"study|trial|academic)\s+sponsor|"
    r"sponsor(?:ship)?\s+(?:sales|prospects?|revenue|packages?|deals?|opportunit)|"
    r"event\s+sponsor|sponsor(?:ship)?\s+(?:of\s+)?(?:events?|conferences?|teams?)|"
    r"sponsor\s+(?:innovation|co-?working|lunch|life insurance|activities)|"
    r"(?:a|your|their)\s+sponsor\s+who|"
    r"sponsor(?:ing|s)?\s+employees\b",
    re.I,
)

# Visa / work-authorisation context. Required for a POSITIVE verdict, because
# "sponsorship available" alone is a phrase an events or sales JD can legitimately
# carry. Not required for the negatives below -- each of those already names the
# act of sponsoring an applicant, and demanding a second signal would lose the
# single most common real phrasing ("we are unable to offer sponsorship for this
# role", which never says the word "visa").
_VISA_CONTEXT = re.compile(
    r"\bvisas?\b|work permit|right to work|work authoris|work authoriz|"
    r"immigration|skilled worker|certificate of sponsorship|home office|"
    r"employment status|work legally|eligib\w+ to work",
    re.I,
)

# "This vacancy will not be sponsored."
#
# _NEG is factored out because the same negations have to reach the sponsoring
# verb by two different routes, and missing one of them was a live bug: the
# first cut required a SECOND "sponsor" token after the verb, which caught
# "unable to OFFER SPONSORSHIP" but not "unable to SPONSOR visas" -- and the
# latter then fell through and matched the POSITIVE pattern on "able to sponsor
# visas" sitting inside "unable to". Six real listings were classified as
# offering sponsorship when they said the exact opposite.
_NEG = (r"(?:unable|not\s+able|cannot|can\s?not|can't|unwilling|won't|will\s+not|"
        r"do\s+not|don't|does\s+not|doesn't|are\s+not|is\s+not|aren't|isn't|"
        r"not\s+in\s+a\s+position)")
_NO_SPONSORSHIP = re.compile(
    # ... unable to SPONSOR (someone / a visa)
    _NEG + r"\s+(?:to\s+|be\s+able\s+to\s+|currently\s+|presently\s+)*sponsor|"
    # ... unable to OFFER/PROVIDE ... sponsorship
    + _NEG + r"\s+(?:to\s+|be\s+able\s+to\s+|currently\s+|presently\s+)*"
        r"(?:offer|provide|support|consider|accept|entertain)\b[^.;|]{0,50}?sponsor|"
    r"no\s+(?:visa\s+|uk\s+|work\s+|skilled worker\s+)*sponsorship\s+"
        r"(?:is\s+)?(?:available|provided|offered|on offer)|"
    r"sponsorship\s*[:\-]?\s*(?:is\s+)?(?:not|un)\s*(?:available|offered|provided|possible)|"
    r"(?:sponsorship|relocation)\s+(?:and\s*/?\s*or\s+\w+\s+)?not\s+provided|"
    r"not\s+(?:be\s+)?(?:eligible|able)\s+for\b[^.;|]{0,40}?sponsorship|"
    r"without\s+(?:the\s+need\s+for\s+)?(?:employer\s+|visa\s+|any\s+)?sponsorship|"
    r"must\s+not\s+require\b[^.;|]{0,40}?sponsorship|"
    r"no\s+sponsorship\b",
    re.I,
)

# "This vacancy can be sponsored." Deliberately much narrower, and gated on
# _VISA_CONTEXT. Real examples in the store are RARE -- a scan of every text-
# bearing row turned up essentially one ("ARQ can sponsor your visa") against
# dozens of negatives -- so this half is built to avoid false positives rather
# than to maximise recall. Note "Tier 2" is pointedly NOT a signal: the store is
# full of "Tier 1 and Tier 2 support", "Tier 2 banks", "tier 2~3 supply base".
# (?<!un) on "able", and (?<!\bno )/(?<!\bnot ) on the bare "sponsorship is
# available" form: "unable to sponsor visas" and "no visa sponsorship available"
# both contain a perfectly good positive as a substring. Belt-and-braces only --
# _NO_SPONSORSHIP is checked first and returns immediately -- but this half must
# be independently safe, because a false "offered" sends someone to spend an
# application on a role that cannot hire them.
_YES_SPONSORSHIP = re.compile(
    r"(?:can|(?<!un)able to|happy to|willing to|will|do|we|open to)\s+"
        r"(?:offer\s+|provide\s+|consider\s+)?sponsor\w*\b[^.;|]{0,40}?"
        r"(?:visa|work permit|skilled worker)|"
    r"(?<!\bno )(?<!\bnot )(?:visa|work permit|skilled worker|uk)\s+sponsorship\s+"
        r"(?:is\s+)?(?:available|offered|provided|considered|on offer)|"
    r"(?<!\bno )(?<!\bnot )sponsorship\s+(?:is\s+)?(?:available|offered|provided)\b|"
    r"(?:we\s+are\s+a|we\s+hold\s+a|holds?\s+a)\s+"
        r"(?:licen[cs]ed\s+sponsor|sponsor\s+licen[cs]e)",
    re.I,
)

_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_SPONSOR_TOKEN = re.compile(r"sponsor\w*", re.I)
# Hard stops that end a clause. A scraped listing's separators matter as much as
# full stops here: a sponsorship note is very often its own bullet or its own
# pipe-delimited header field.
_CLAUSE_BREAK = re.compile(r"[.!?;|•\n\r]")

# How far either side of a "sponsor" token a verdict may look. Bounded because
# an unsplit header blob ("Senior AI Engineer - London - 150-190k - ... ") can
# run for hundreds of characters with no full stop in it, and a negation at one
# end has nothing to do with a "sponsorship available" at the other.
_WINDOW_BEFORE = 90
_WINDOW_AFTER = 90
STATEMENT_QUOTE_MAX = 220


def _plain(text: str) -> str:
    plain = _TAGS.sub(" ", text or "")
    plain = (plain.replace("&nbsp;", " ").replace("&amp;", "&")
                  .replace("&#39;", "'").replace("&quot;", '"'))
    return _WS.sub(" ", plain).strip()


def _window(text: str, start: int, end: int) -> str:
    """The clause around a match: bounded by distance AND by the nearest clause
    break on each side, whichever is closer."""
    lo = max(0, start - _WINDOW_BEFORE)
    hi = min(len(text), end + _WINDOW_AFTER)
    before = text[lo:start]
    brk = list(_CLAUSE_BREAK.finditer(before))
    if brk:
        lo += brk[-1].end()
    after = text[end:hi]
    m = _CLAUSE_BREAK.search(after)
    if m:
        hi = end + m.start()
    return text[lo:hi].strip()


def statement_in_text(text: str) -> tuple[str, str] | None:
    """What the listing SAYS about sponsoring this vacancy.

    Returns ("not_offered" | "offered", the clause it said it in), or None --
    and None is by far the most common answer (12,188 of 12,273 store rows),
    meaning the listing was silent. Silence is never read as either verdict.

    CLAUSE-SCOPED, not document-scoped, and not sentence-scoped either. A
    whole-document search would happily pair a "not eligible" in the benefits
    section with a "sponsorship" three paragraphs away. Sentences are not enough
    on their own because scraped listings routinely contain header blobs with no
    full stop for 300 characters -- so the window is bounded by distance as well,
    and the returned quote is that window, which is also what makes the card's
    quote readable rather than starting 200 chars before the claim.

    NEGATIVE WINS TIES, checked first and returned immediately. A listing
    carrying both a general "we can sponsor" and a specific "we are unable to
    offer sponsorship for this role" is telling the candidate this role is not
    sponsored, and the two errors do not cost the same: a false "offered" sends
    someone to spend an application on a role that cannot hire them."""
    if not text or "sponsor" not in text.lower():
        return None

    plain = _plain(text)
    positive: str | None = None
    for m in _SPONSOR_TOKEN.finditer(plain):
        clause = _window(plain, m.start(), m.end())
        if not clause or _SPONSOR_SENSE_EXCLUDE.search(clause):
            continue  # a benefits / stakeholder / events use of the word
        if _NO_SPONSORSHIP.search(clause):
            return "not_offered", clause[:STATEMENT_QUOTE_MAX]
        if positive is None and _VISA_CONTEXT.search(clause) and _YES_SPONSORSHIP.search(clause):
            positive = clause[:STATEMENT_QUOTE_MAX]
    return ("offered", positive) if positive else None
