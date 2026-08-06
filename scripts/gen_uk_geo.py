"""Dev-only generator for the app's UK geography reference data.

Runtime code must NOT call a geocoding API -- this script is run by hand when we
want to refresh coverage, and it writes a plain-data module the app imports:

  * backend/app/uk_geo_gen.py  (imported by backend/app/services/geo.py)

Same posture as scripts/gen_countries.py: one dev-time source of truth, one
generated plain-data module, zero runtime dependencies and zero per-search API
calls. Distance is then pure arithmetic (haversine) over a dict lookup.

WHY OUTCODES RATHER THAN FULL POSTCODES
The full ONS Postcode Directory is ~1.8M rows / ~1GB -- unusable as a committed
Python module, and far more precision than a commute filter needs. The OUTWARD
code (the "B70" of "B70 6AW") is the coarsest unit that still localises to a few
km, and there are only ~3,000 of them, which fits comfortably in one file. A
commute radius is chosen in tens of miles, so outcode-centroid error is well
inside the noise.

SOURCE
api.postcodes.io -- a free, unauthenticated, no-rate-limit service whose outcode
centroids are computed straight from the ONS Postcode Directory (OGL v3, the
same open dataset), and whose place names come from OS Open Names. We crawl it
once here rather than at runtime.

There is no bulk "list every outcode" endpoint, so the crawl is a breadth-first
walk over /outcodes/{outcode}/nearest: each response names up to 100 outcodes
near the one asked about, so following every newly-discovered outcode closes
over the whole (geographically contiguous) UK from a handful of seeds. It
terminates because the outcode set is finite and each node is expanded once.

Run:  venv/Scripts/python scripts/gen_uk_geo.py
Deps: requests (already a runtime dep)
"""
from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "backend" / "app" / "uk_geo_gen.py"
# Raw crawl results, so re-running only to change how the gazetteer is BUILT
# doesn't re-hit the API. Delete it to force a fresh crawl.
CACHE_PATH = ROOT / "scripts" / ".uk_geo_crawl_cache.json"

# radius is load-bearing, not a tuning knob: the endpoint's DEFAULT search
# radius is only a few km, which returns ~20 neighbours in a city and lets the
# breadth-first walk close over a subset of the country (a first attempt
# terminated at 1,005 of ~3,000 outcodes). 25km is the endpoint's maximum and
# fills the 100-result limit almost everywhere, so the walk actually spreads.
API = "https://api.postcodes.io/outcodes/{}/nearest?limit=100&radius=25000"
WORKERS = 8

# Spread across the four nations plus the offshore areas, so a crawl can't be
# left stranded by a weak link in the nearest-neighbour graph. Any valid outcode
# works as a seed; these are simply well-separated ones.
SEEDS = [
    "EC1A", "SW1A", "B1", "M1", "LS1", "L1", "S1", "NE1", "BS1", "CF10",
    "EH1", "G1", "AB10", "IV1", "BT1", "PL1", "NR1", "CB1", "SA1", "LL11",
    "KW1", "ZE1", "HS1", "IM1", "JE1", "GY1", "TR1", "CA1", "DG1", "KA1",
]

# Place names that are too coarse for a distance calculation: a listing saying
# "UK" or "England" states no place, and resolving it to a national centroid
# would manufacture a precise-looking distance out of nothing. Consumers must
# see these as UNKNOWN (no chip, no filter decision), so they never enter the
# gazetteer.
COUNTRY_LEVEL = {
    "england", "scotland", "wales", "northern ireland", "united kingdom", "uk",
    "great britain", "britain", "channel islands", "isle of man",
}

# Names the ONS/OS datasets don't carry as a single place, but which listings
# use constantly. London is the big one: its outcodes sit under borough names
# ("Camden", "Southwark"), so the word "London" itself would otherwise resolve
# to nothing at all. Coordinates are the conventional city centroids.
MANUAL_PLACES = {
    "london": (51.5074, -0.1278),
    "greater london": (51.5074, -0.1278),
    "central london": (51.5142, -0.0931),
    "city of london": (51.5155, -0.0922),
    "west london": (51.5099, -0.2200),
    "east london": (51.5400, -0.0200),
    "north london": (51.5800, -0.1300),
    "south london": (51.4500, -0.1100),
}

_UNPARISHED_RE = re.compile(r",\s*unparished area\s*$", re.I)


def _norm(name: str) -> str:
    """Gazetteer key: casefolded, accent-stripped, whitespace-collapsed."""
    s = unicodedata.normalize("NFKD", (name or "").strip())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).lower()


def _place_label(rec: dict) -> str:
    """The human place name for an outcode centroid.

    Parish first, district second. A parish is the actual town ("Farnham"),
    while the district is the local authority that contains it ("Waverley") --
    a listing says the former and a candidate recognises it. Where a place has
    no parish the ONS records it as "<District>, unparished area", which strips
    back to exactly the name we want ("Leeds, unparished area" -> "Leeds")."""
    for parish in rec.get("parish") or []:
        stripped = _UNPARISHED_RE.sub("", parish or "").strip()
        if stripped:
            return stripped
    for district in rec.get("admin_district") or []:
        if (district or "").strip():
            return district.strip()
    return ""


def crawl() -> dict[str, dict]:
    """outcode -> its postcodes.io record, for every outcode reachable from SEEDS."""
    found: dict[str, dict] = {}
    frontier = list(dict.fromkeys(SEEDS))
    session = requests.Session()

    def fetch(outcode: str) -> list[dict]:
        try:
            r = session.get(API.format(outcode), timeout=20)
            if r.status_code != 200:
                return []
            return r.json().get("result") or []
        except Exception:
            return []

    rounds = 0
    while frontier:
        rounds += 1
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            batches = list(ex.map(fetch, frontier))
        next_frontier: list[str] = []
        for batch in batches:
            for rec in batch:
                oc = (rec.get("outcode") or "").strip().upper()
                if not oc or oc in found:
                    continue
                if rec.get("latitude") is None or rec.get("longitude") is None:
                    continue
                found[oc] = rec
                next_frontier.append(oc)
        print(f"  round {rounds}: +{len(next_frontier)} (total {len(found)})", flush=True)
        frontier = next_frontier
    return found


# Two points this far apart are treated as different places that happen to
# share a name, not as one sprawling place. Comfortably larger than any UK
# parish or district and comfortably smaller than the gap between same-named
# towns (the Farnhams below are 30-130 miles apart).
_CLUSTER_SPLIT_MILES = 25.0


def _miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 3958.7613 * math.asin(min(1.0, math.sqrt(h)))


def _cluster(points: list[tuple[float, float, str]]) -> list[dict]:
    """Single-link agglomeration of one name's points into distinct places.

    Necessary because place names repeat across the UK and AVERAGING THEM IS
    WRONG: a first cut took the mean of every outcode naming "Farnham" and put
    it 34 miles from the Surrey town, in a field near Hemel Hempstead, because
    Essex/Dorset/North Yorkshire each have a Farnham too. The mean of several
    real places is reliably a place that does not exist."""
    clusters: list[dict] = []
    for lat, lon, qualifier in points:
        for c in clusters:
            if _miles((lat, lon), (c["lat_sum"] / c["n"], c["lon_sum"] / c["n"])) <= _CLUSTER_SPLIT_MILES:
                c["lat_sum"] += lat
                c["lon_sum"] += lon
                c["n"] += 1
                if qualifier:
                    c["qualifiers"].add(qualifier)
                break
        else:
            clusters.append({"lat_sum": lat, "lon_sum": lon, "n": 1,
                             "qualifiers": {qualifier} if qualifier else set()})
    for c in clusters:
        c["lat"] = round(c["lat_sum"] / c["n"], 4)
        c["lon"] = round(c["lon_sum"] / c["n"], 4)
    clusters.sort(key=lambda c: -c["n"])
    return clusters


def build_places(records: dict[str, dict]) -> tuple[dict, dict]:
    """(PLACE_COORDS, PLACE_VARIANTS) -- the gazetteer, harvested from the parish
    / district / county fields of the outcodes already fetched, so it costs no
    extra requests.

    A name whose points form ONE cluster is unambiguous and lands in
    PLACE_COORDS. A name with several (see _cluster) lands in PLACE_VARIANTS as
    an ordered list of {lat, lon, qualifiers}, largest first, so the resolver can
    disambiguate on a county/district also named in the same location string
    ("Farnham, Surrey") and fall back to the largest only when it is a strict
    plurality. Cluster size is counted in outcodes, which is a rough proxy for
    how built-up a place is -- good enough to prefer Farnham, Surrey (2 outcodes)
    over Farnham, Dorset (1), and deliberately not trusted when it ties."""
    points: dict[str, list[tuple[float, float, str]]] = {}

    def add(name: str, lat: float, lon: float, qualifier: str) -> None:
        key = _norm(_UNPARISHED_RE.sub("", name or ""))
        if not key or key in COUNTRY_LEVEL:
            return
        points.setdefault(key, []).append((lat, lon, qualifier))

    for rec in records.values():
        lat, lon = float(rec["latitude"]), float(rec["longitude"])
        # What a listing would add after the town to disambiguate it. County
        # first (that is how people write it); district where there is no
        # county, which is the norm for unitary authorities.
        counties = [c for c in (rec.get("admin_county") or []) if c]
        districts = [d for d in (rec.get("admin_district") or []) if d]
        qualifier = _norm(_UNPARISHED_RE.sub("", (counties or districts or [""])[0]))
        for field in ("parish", "admin_district", "admin_county"):
            for raw in rec.get(field) or []:
                add(raw, lat, lon, qualifier)
        # The outcode's own label, so a place whose only appearance is as a
        # stripped "unparished area" is still directly searchable.
        add(_place_label(rec), lat, lon, qualifier)

    coords: dict[str, tuple[float, float]] = {}
    variants: dict[str, list] = {}
    for name, pts in points.items():
        clusters = _cluster(pts)
        if len(clusters) == 1:
            coords[name] = (clusters[0]["lat"], clusters[0]["lon"])
        else:
            variants[name] = [
                (c["lat"], c["lon"], sorted(q for q in c["qualifiers"] if q), c["n"])
                for c in clusters
            ]
    # Manual entries are authoritative: they exist precisely because the dataset
    # has no single record for them, so a harvested cluster must not shadow one.
    for k, v in MANUAL_PLACES.items():
        key = _norm(k)
        coords[key] = v
        variants.pop(key, None)
    return coords, variants


def render(records: dict[str, dict], places: dict, variants: dict) -> str:
    coords = {oc: (round(float(r["latitude"]), 4), round(float(r["longitude"]), 4))
              for oc, r in sorted(records.items())}
    labels = {oc: _place_label(r) for oc, r in sorted(records.items()) if _place_label(r)}

    lines = [
        '"""AUTO-GENERATED by scripts/gen_uk_geo.py -- do not edit by hand.',
        "",
        "UK geography reference data for the commute-distance filter and for",
        "rendering a raw postcode as a place name. Regenerate with:",
        "    venv/Scripts/python scripts/gen_uk_geo.py",
        "",
        "Derived from api.postcodes.io, whose centroids come from the ONS Postcode",
        "Directory (Open Government Licence v3) and whose place names come from OS",
        "Open Names. Outcode-level precision only -- see scripts/gen_uk_geo.py.",
        '"""',
        "",
        "# Outward postcode -> (latitude, longitude) centroid.",
        "OUTCODE_COORDS = {",
    ]
    lines += [f'"{oc}": ({la}, {lo}),' for oc, (la, lo) in coords.items()]
    lines += [
        "}",
        "",
        "# Outward postcode -> the human place name to show instead of the raw code.",
        "OUTCODE_PLACE = {",
    ]
    lines += [f'"{oc}": {label!r},' for oc, label in labels.items()]
    lines += [
        "}",
        "",
        "# Normalised place name (casefolded, accent-stripped) -> (latitude, longitude),",
        "# for names that denote exactly ONE place in the UK.",
        "# Deliberately excludes country-level names: 'UK' states no place, and giving",
        "# it a centroid would invent a distance the listing never claimed.",
        "PLACE_COORDS = {",
    ]
    lines += [f'{name!r}: ({la}, {lo}),' for name, (la, lo) in sorted(places.items())]
    lines += [
        "}",
        "",
        "# Names shared by several real UK places -> every place that bears them,",
        "# largest first, as (latitude, longitude, [qualifier names], outcode_count).",
        "# The qualifiers are the county (or unitary district) a listing would write",
        "# after the town to disambiguate it. Averaging these into one point is what",
        "# this structure exists to prevent -- see scripts/gen_uk_geo.py's _cluster.",
        "PLACE_VARIANTS = {",
    ]
    lines += [f'{name!r}: {v!r},' for name, v in sorted(variants.items())]
    lines += ["}", ""]
    return "\n".join(lines)


def main() -> int:
    if CACHE_PATH.exists():
        print(f"Reusing crawl cache {CACHE_PATH.name} (delete it to re-crawl)...", flush=True)
        records = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    else:
        print("Crawling postcodes.io for outcode centroids...", flush=True)
        records = crawl()
        if len(records) < 2000:
            print(f"ERROR: only {len(records)} outcodes found -- expected ~3,000. "
                  f"Refusing to write a truncated dataset.", file=sys.stderr)
            return 1
        CACHE_PATH.write_text(json.dumps(records), encoding="utf-8")
    places, variants = build_places(records)
    OUT_PATH.write_text(render(records, places, variants), encoding="utf-8")
    print(f"Wrote {OUT_PATH} -- {len(records)} outcodes, {len(places)} unambiguous "
          f"places, {len(variants)} ambiguous ({OUT_PATH.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
