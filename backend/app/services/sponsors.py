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
