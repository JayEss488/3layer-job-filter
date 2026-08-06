"""Turning a listing's location STRING into a point on the map, and a distance.

Until this existed the app had exactly two location concepts -- "same country"
(engine._filter_by_country) and "the candidate's city name appears as a
substring" (engine._filter_by_local_place) -- and nothing in between. Commute
distance is the first filter a real job seeker applies, and it was the one thing
the app could not express at all: a Newcastle role and a next-street role were
indistinguishable to every stage of the pipeline.

It also fixes the display side of the same gap. Board location fields routinely
carry a bare postcode ("B706AW", "LS101EY", "GU98AD"), which rendered verbatim
on a result card and simply reads as a bug.

DESIGN
* No API and no LLM. Every lookup is a dict hit against ../uk_geo_gen.py, a
  committed plain-data module built once by scripts/gen_uk_geo.py from ONS
  postcode centroids. A search run resolves thousands of locations; anything
  per-call-billed or network-bound would be the wrong shape entirely.
* Outward-code precision (the "B70" of "B70 6AW"), because a commute radius is
  chosen in tens of miles and the full 1.8M-row postcode file is neither
  committable nor needed. Distances are therefore reported in whole miles and
  should never be presented as more exact than that.
* UNRESOLVED IS A FIRST-CLASS ANSWER, and the common one. "UK", "Remote",
  "Home-based" and every non-UK location resolve to None, and None must always
  mean "no distance information" -- never "far away". Every consumer here treats
  it as a reason to say nothing, never a reason to drop a listing.

SCOPE: UK only. The dataset is the ONS postcode directory, so a profile based
elsewhere resolves nothing and the distance features are silently inert for
them -- no wrong answers, just no chip and no distance filter. Widening this
means a second dataset in gen_uk_geo.py's shape, not a change here.
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

from ..uk_geo_gen import OUTCODE_COORDS, OUTCODE_PLACE, PLACE_COORDS, PLACE_VARIANTS

EARTH_RADIUS_MILES = 3958.7613

# A UK postcode's INWARD half is always exactly digit + 2 letters, so a postcode
# is parsed from the right: strip the inward code and whatever precedes it is
# the outward code. Parsing from the left instead needs the full outward grammar
# (A9, A99, A9A, AA9, AA99, AA9A) and gets "LS101EY" wrong -- it reads "LS1"
# and leaves "01EY", when the answer is LS10.
_FULL_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\s*(\d[A-Z]{2})\b", re.I)
# An outward code standing alone ("Leeds LS10"). Kept separate from the pattern
# above and validated against the dataset before being trusted, because on its
# own this shape also matches ordinary words and codes.
_OUTCODE_ONLY_RE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?)\b", re.I)

# Splits a location string into the parts a listing actually separates: "London,
# Greater London / Hybrid". Hyphens are NOT separators -- plenty of real place
# names contain them ("Southend-on-Sea", "Stoke-on-Trent").
_PART_SPLIT_RE = re.compile(r"[,/;|()]+|\s+-\s+")
_NON_NAME_RE = re.compile(r"[^a-z0-9\s'-]+")


def haversine_miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in miles between two (lat, lon) pairs."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * EARTH_RADIUS_MILES * math.asin(min(1.0, math.sqrt(h)))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", _NON_NAME_RE.sub(" ", (text or "").lower())).strip()


def extract_outcode(text: str) -> str | None:
    """The outward postcode named in `text`, or None.

    Accepts every form boards emit: "B70 6AW", "B706AW", "b70" and a postcode
    embedded in a longer string. An outward code that isn't in the dataset is
    rejected rather than guessed at, which is also what keeps the bare-outcode
    pattern from firing on ordinary text."""
    if not text:
        return None
    m = _FULL_POSTCODE_RE.search(text)
    if m:
        outcode = m.group(1).upper()
        if outcode in OUTCODE_COORDS:
            return outcode
    for m in _OUTCODE_ONLY_RE.finditer(text):
        outcode = m.group(1).upper()
        if outcode in OUTCODE_COORDS:
            return outcode
    return None


def _place_candidates(text: str) -> list[str]:
    """Every plausible place name in a location string, most specific first.

    Each comma-separated part is tried whole before its sub-phrases, and longer
    word windows before shorter ones, so "Newcastle upon Tyne" is matched as
    itself rather than as "Newcastle"."""
    out: list[str] = []
    for part in _PART_SPLIT_RE.split(text or ""):
        words = _norm(part).split()
        if not words:
            continue
        for size in range(len(words), 0, -1):
            for start in range(0, len(words) - size + 1):
                phrase = " ".join(words[start:start + size])
                if phrase not in out:
                    out.append(phrase)
    return out


def _pick_variant(name: str, text: str) -> tuple[float, float] | None:
    """Choose between the several real UK places sharing `name`.

    Prefers a variant whose county/district is ALSO named in the same location
    string -- "Farnham, Surrey" is not ambiguous to a human and shouldn't be
    here. Failing that, falls back to the largest variant, but only when it is a
    STRICT plurality: two equally-sized candidates mean the string genuinely
    doesn't say which, and inventing a coin-flip answer would put a listing tens
    of miles from where it actually is. Returning None there is correct -- the
    caller treats it as "no distance known"."""
    variants = PLACE_VARIANTS.get(name) or []
    if not variants:
        return None
    normalised = _norm(text)
    for lat, lon, qualifiers, _n in variants:
        if any(q and q in normalised for q in qualifiers):
            return (lat, lon)
    if len(variants) > 1 and variants[0][3] == variants[1][3]:
        return None
    return (variants[0][0], variants[0][1])


@lru_cache(maxsize=8192)
def resolve(text: str) -> tuple[float, float] | None:
    """(lat, lon) for a location string, or None when it names no UK place.

    Postcode first (it is unambiguous and exact), then the place gazetteer.
    Cached because a run resolves the same handful of city names thousands of
    times."""
    if not text or not text.strip():
        return None
    outcode = extract_outcode(text)
    if outcode:
        return OUTCODE_COORDS[outcode]
    for name in _place_candidates(text):
        hit = PLACE_COORDS.get(name)
        if hit:
            return hit
        if name in PLACE_VARIANTS:
            picked = _pick_variant(name, text)
            if picked:
                return picked
    return None


def distance_miles(origin: str, destination: str) -> float | None:
    """Whole miles between two location strings, or None if either is unknown.

    None is not zero and not infinity: it means the question can't be answered
    from what the listing said, so callers must neither penalise nor reward it."""
    a, b = resolve(origin), resolve(destination)
    if a is None or b is None:
        return None
    return round(haversine_miles(a, b))


def pretty_location(text: str) -> str | None:
    """A location string with raw postcodes made human, or None to leave it be.

    Three cases, in order:
      * no postcode in the string      -> None (nothing to fix)
      * postcode alongside real words  -> the words, postcode dropped as noise
                                          ("West Bromwich, B70 6AW" -> "West Bromwich")
      * postcode and nothing else      -> the ONS place name for that outcode
                                          ("B706AW" -> "Sandwell")
    An unknown outcode falls through to None: showing the raw code is worse than
    ideal, but inventing a place name for it would be worse still."""
    if not text or not _FULL_POSTCODE_RE.search(text) and not extract_outcode(text):
        return None
    stripped = _FULL_POSTCODE_RE.sub(" ", text)
    outcode = extract_outcode(text)
    if outcode:
        stripped = re.sub(rf"\b{re.escape(outcode)}\b", " ", stripped, flags=re.I)
    remainder = re.sub(r"\s+", " ", re.sub(r"[\s,/;|-]+$|^[\s,/;|-]+", "", stripped)).strip(" ,/;|-")
    remainder = re.sub(r"\s*,\s*,+", ", ", remainder).strip(" ,")
    if len(re.sub(r"[^a-z]", "", remainder.lower())) >= 2:
        return remainder
    if outcode and OUTCODE_PLACE.get(outcode):
        return OUTCODE_PLACE[outcode]
    return None
