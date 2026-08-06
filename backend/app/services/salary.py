"""Turning whatever a board said about pay into comparable numbers.

Pay reached the app as free text and stayed that way. A measured sample of the
roles actually surfaced to a user carried, among others:

    "£28,505 to £34,613"          "£30,000 (rising to £45,000)"
    "£32,000 - £35,000 per annum, inc benefits"   "up to 70k"
    "GBP 28800 - 48000 per year"  "£200 per day"
    "Circa 30,000"                "Competitive salary, up to 12% bonus"

Nothing could compare two of those, sort by them, or convert between them, and
the candidate's own salary floor was being enforced against `salary_max` --
a raw number from a source that may have meant per hour. A £25/hour role
(≈£48,750 a year) was being dropped for a candidate with a £30,000 floor.

WHAT THIS MODULE PROMISES
* A period is either STATED by the text/source or inferred from magnitude, and
  the inference is coarse and documented (see _infer_period). It is never
  silently assumed to be annual.
* Percentages are never salary. "up to 12% bonus" contains the number 12 and
  means nothing about pay; a parser that reads left-to-right for digits gets
  this wrong, so % figures are stripped before any number is read.
* Returning None is normal and correct. "Competitive", "Negotiable" and
  "National Minimum Wage" state no figure, and inventing one would be worse
  than the free text they already had -- callers keep showing that instead.
* Currency is recorded but NEVER converted. A cross-currency comparison needs a
  live FX rate this app has no business fetching per search; magnitudes are
  compared as-is, exactly as engine._filter_by_salary already did, and that
  coarseness is stated at the call site rather than hidden here.

WHAT `text` MAY BE, AND WHAT IT MAY NOT
`text` must be a SALARY FIELD -- a board's salary string, the judge's role_salary,
or engine._salary_text's currency-anchored range extract. It must NEVER be a job
description or snippet. This is not a stylistic preference: run over the snippets
of a live 8,050-row store, this parser "found" pay in 4,389 of them, and a
15-listing audit of those found 12 wrong. What it reads out of prose is employee
counts ("270+ locations and 4,000+ employees" -> £4,000/year), requisition
numbers ("Requisition Number: 51630" -> £51,630/year), years of experience
("5 years ... at least 3 years" -> £3-6/hour) and signing bonuses ("$1,000 new
hire bonus" -> $1,000/day). Every one of those is inside the plausibility bounds
below, because the bounds can only reject impossible SALARIES -- they cannot
tell a salary from any other number of similar size. Only the surrounding field
can do that, and by the time the text is here that context is gone.
"""
from __future__ import annotations

import re

# Annualisation factors. Deliberately conventional UK full-time assumptions
# rather than anything derived per-listing: a listing that says "£15 per hour"
# does not say how many hours, so any annual figure is an ESTIMATE and is only
# ever used for coarse comparison (the salary floor) and for the card's
# hourly/yearly toggle, never presented as the employer's own number.
#   37.5h/week x 52 weeks = 1950 hours; 5 days/week x 52 = 260 days.
HOURS_PER_YEAR = 1950
DAYS_PER_YEAR = 260
WEEKS_PER_YEAR = 52
MONTHS_PER_YEAR = 12

ANNUAL_MULTIPLIER = {
    "year": 1,
    "month": MONTHS_PER_YEAR,
    "week": WEEKS_PER_YEAR,
    "day": DAYS_PER_YEAR,
    "hour": HOURS_PER_YEAR,
}

PERIODS = tuple(ANNUAL_MULTIPLIER)

_CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "₹": "INR", "¥": "JPY"}
_CURRENCY_CODES = ("GBP", "USD", "EUR", "AUD", "CAD", "NZD", "INR", "PLN",
                   "CHF", "SEK", "NOK", "DKK", "ZAR", "SGD", "JPY")

# Period wording as boards actually write it. Ordered longest-first within each
# period so "per annum" can't be matched by a stray "annum" rule, and checked
# against the whole string.
_PERIOD_PATTERNS: list[tuple[str, str]] = [
    ("hour", r"per\s*hour|hourly|/\s*h(?:r|our)?\b|\bp\.?/?h\b|an\s+hour|ph\b"),
    ("day", r"per\s*day|daily|/\s*day\b|a\s+day|\bp\.?d\.?\b|day\s*rate"),
    ("week", r"per\s*week|weekly|/\s*w(?:k|eek)?\b|a\s+week|\bp\.?w\.?\b"),
    ("month", r"per\s*month|monthly|/\s*mo(?:nth)?\b|a\s+month|\bp\.?c\.?m\.?\b|\bpm\b"),
    ("year", r"per\s*annum|per\s*year|annually|yearly|/\s*(?:yr|year|annum)\b|"
             r"\bp\.?a\.?\b|a\s+year|per\s*ann\b"),
]

# Anything attached to a % is a bonus/uplift/pension figure, not pay. Stripped
# before numbers are read -- see the module docstring.
_PERCENT_RE = re.compile(r"\d[\d.,]*\s*%")
# 70k / 70K / 28.5k
_K_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*k\b", re.I)
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# "up to X" / "from X" / "circa X" -- one number that is a bound rather than a
# point. Which bound differs, and getting it backwards turns a ceiling into a
# floor, so they're matched explicitly rather than lumped together.
_MAX_ONLY_RE = re.compile(r"\b(?:up\s*to|maximum\s*of|max\b|no\s*more\s*than|to)\b", re.I)
_MIN_ONLY_RE = re.compile(r"\b(?:from|starting\s*(?:at|from)|at\s*least|minimum\s*of|min\b|\+)\b", re.I)

# Plausibility bounds per period, in the listing's own currency units. A figure
# outside these is far more likely to be a reference number, a headcount or a
# postcode fragment that survived the number scan than it is to be pay -- and a
# wrong salary is worse than no salary, because the floor filter acts on it.
_PLAUSIBLE = {
    "hour": (3, 500),
    "day": (30, 5000),
    "week": (100, 20000),
    "month": (300, 100000),
    "year": (3000, 2000000),
}


def normalise_period(raw: str | None) -> str | None:
    """A source's own period field ('HOUR', 'yearly', 'PER_ANNUM') -> our vocab."""
    if not raw:
        return None
    text = str(raw).strip().lower()
    for period, pattern in _PERIOD_PATTERNS:
        if re.search(pattern, text):
            return period
    for period in PERIODS:
        if period in text:
            return period
    return {"annum": "year", "annual": "year", "yr": "year",
            "hr": "hour", "wk": "week", "mo": "month"}.get(text)


def normalise_currency(raw: str | None) -> str | None:
    if not raw:
        return None
    text = str(raw).strip().upper()
    if text in _CURRENCY_CODES:
        return text
    return _CURRENCY_SYMBOLS.get(text.strip())


def _detect_currency(text: str) -> str | None:
    for symbol, code in _CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    upper = text.upper()
    for code in _CURRENCY_CODES:
        # No trailing \b: boards write the code hard against the figure
        # ("PLN10,800"), and N-to-1 is not a word boundary, so \b lost those.
        # The negative lookahead keeps it from firing inside a longer word.
        if re.search(rf"\b{code}(?![A-Z])", upper):
            return code
    return None


def _detect_period(text: str) -> str | None:
    lowered = text.lower()
    for period, pattern in _PERIOD_PATTERNS:
        if re.search(pattern, lowered):
            return period
    return None


def _infer_period(amount: float) -> str:
    """Period for a figure whose text never said one -- purely by magnitude.

    Crude by necessity and safe in practice, because the bands are far apart:
    nobody is paid £45,000 an hour and nobody is paid £22 a year. The 1000
    boundary is the one that matters (annual vs everything else) and it is the
    one with the most headroom on both sides."""
    if amount >= 1000:
        return "year"
    if amount >= 100:
        return "day"
    return "hour"


def _plausible(amount: float, period: str) -> bool:
    low, high = _PLAUSIBLE.get(period, (0, float("inf")))
    return low <= amount <= high


def _numbers(text: str) -> list[float]:
    """Every money-shaped figure in `text`, in order, percentages excluded."""
    cleaned = _PERCENT_RE.sub(" ", text)
    found: list[tuple[int, float]] = []
    for m in _K_NUMBER_RE.finditer(cleaned):
        found.append((m.start(), float(m.group(1)) * 1000))
    # Blank out the k-figures so the plain scan doesn't re-read "70" from "70k".
    cleaned = _K_NUMBER_RE.sub(lambda m: " " * len(m.group(0)), cleaned)
    for m in _NUMBER_RE.finditer(cleaned):
        raw = m.group(0).replace(",", "")
        try:
            found.append((m.start(), float(raw)))
        except ValueError:
            continue
    return [amount for _pos, amount in sorted(found)]


def parse_salary(
    text: str | None = None,
    *,
    minimum=None,
    maximum=None,
    period: str | None = None,
    currency: str | None = None,
) -> dict | None:
    """Normalise pay into {min, max, period, currency}, or None if none stated.

    Structured source figures (`minimum`/`maximum`, e.g. Reed's
    minimumSalary/maximumSalary or Adzuna's salary_min/salary_max) take priority
    over `text`: they are the board's own fields rather than a regex's reading of
    prose. `text` is still parsed for the period and currency when the source
    didn't supply them, since most boards give numbers with no unit at all.

    `text` MUST be a salary field, never a job description -- see the module
    docstring for what happens otherwise, and why the plausibility bounds can't
    save you from it.

    `min` and `max` are in the returned `period`'s units, NOT annualised -- see
    to_annual. Either may be None ("up to £30,000" has no floor); both being
    None means nothing was found and the function returns None instead."""
    text = (text or "").strip()
    stated_period = normalise_period(period) or (_detect_period(text) if text else None)
    stated_currency = normalise_currency(currency) or (_detect_currency(text) if text else None)

    lo = hi = None
    for value, target in ((minimum, "lo"), (maximum, "hi")):
        try:
            amount = float(value) if value is not None else None
        except (TypeError, ValueError):
            amount = None
        if amount is not None and amount > 0:
            if target == "lo":
                lo = amount
            else:
                hi = amount

    if lo is None and hi is None and text:
        amounts = [a for a in _numbers(text) if a > 0]
        if amounts:
            resolved = stated_period or _infer_period(max(amounts))
            amounts = [a for a in amounts if _plausible(a, resolved)]
        if amounts:
            if len(amounts) >= 2:
                lo, hi = min(amounts), max(amounts)
            elif _MAX_ONLY_RE.search(text) and not _MIN_ONLY_RE.search(text):
                hi = amounts[0]
            elif _MIN_ONLY_RE.search(text):
                lo = amounts[0]
            else:
                # A bare single figure ("£35,000", "Circa 30,000") is the role's
                # pay, not a one-sided bound: recording it as both ends is what
                # lets it be compared and displayed at all.
                lo = hi = amounts[0]

    if lo is None and hi is None:
        return None
    resolved_period = stated_period or _infer_period(hi if hi is not None else lo)
    if not all(_plausible(a, resolved_period) for a in (lo, hi) if a is not None):
        return None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return {"min": lo, "max": hi, "period": resolved_period, "currency": stated_currency}


def to_annual(amount: float | None, period: str | None) -> float | None:
    """A figure in `period`'s units as a full-time-equivalent annual figure.

    An ESTIMATE for anything but "year" -- see HOURS_PER_YEAR. Used for coarse
    comparison (the candidate's salary floor) and for the card's period toggle;
    never stored, and never shown as though the employer stated it."""
    if amount is None:
        return None
    multiplier = ANNUAL_MULTIPLIER.get(period or "year")
    return amount * multiplier if multiplier else None
