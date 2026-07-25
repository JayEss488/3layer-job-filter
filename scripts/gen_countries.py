"""Dev-only generator for the app's country reference data.

Runtime code must NOT import geonamescache / pycountry -- this script is run by
hand when we want to refresh coverage, and it writes plain-data modules that the
app imports instead:

  * countries_data.py            (repo root, imported by full_auto.py)
  * backend/app/countries_gen.py (imported by backend/app/config.py)
  * frontend/lib/countries.gen.ts (imported by LocationPicker.tsx)

All three are generated from ONE source of truth here, so the country dropdown,
the hard country filter's tokens, and the frontend auto-detect map can never
drift apart again (they used to be four separately hand-maintained lists that
only covered 13 countries -- see the country-coverage work).

Run:  venv/Scripts/python scripts/gen_countries.py
Deps (dev only):  pip install geonamescache pycountry
"""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import geonamescache
import pycountry

ROOT = Path(__file__).resolve().parent.parent

# Adzuna operates only in these ~19 country nodes; every other cc must skip the
# Adzuna call (it returns UNSUPPORTED_COUNTRY otherwise). This is an Adzuna fact,
# not a dataset one, so it's hard-coded here.
ADZUNA_SUPPORTED = {
    "gb", "us", "ca", "au", "de", "fr", "in", "it", "nl", "at", "pl", "sg",
    "za", "br", "mx", "es", "nz", "be", "ch",
}

# High-signal aliases the raw datasets don't carry as a country "name".
MANUAL_ALIASES = {
    "gb": ["uk", "u.k.", "great britain", "britain", "england", "scotland",
           "wales", "northern ireland"],
    "us": ["usa", "u.s.", "u.s.a.", "america", "united states of america"],
    "ae": ["uae", "u.a.e."],
    "nl": ["holland"],
    "de": ["deutschland"],
    "kr": ["south korea", "korea"],
    "kp": ["north korea"],
    "cz": ["czech republic", "czechia"],
    "ru": ["russia"],
    "ir": ["iran"],
    "sy": ["syria"],
    "tz": ["tanzania"],
    "bo": ["bolivia"],
    "ve": ["venezuela"],
    "vn": ["vietnam"],
    "la": ["laos"],
    "md": ["moldova"],
    "hk": ["hong kong"],
    "tw": ["taiwan"],
}

# A city token shorter than this, or in this stoplist, is too collision-prone to
# use as a substring/word signal.
MIN_CITY_LEN = 4
CITY_STOPWORDS = {"city", "town", "west", "east", "north", "south", "central",
                  "district", "county", "state", "port", "saint", "santa",
                  "san", "new", "old", "lake", "hill", "park", "remote"}

CITY_POP_THRESHOLD = 200_000
MAX_CITIES_PER_COUNTRY = 20
# Frontend auto-detect map stays small (dropdown covers the rest): country
# name/aliases + this many top cities per country.
FE_CITIES_PER_COUNTRY = 6


def _ascii(s: str) -> str:
    """Lowercase, strip accents/diacritics, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def build() -> dict:
    gc = geonamescache.GeonamesCache()
    gc_countries = gc.get_countries()          # ISO2 -> {name, capital, ...}
    gc_cities = gc.get_cities()                # id -> {name, countrycode, population}

    # ISO2 codes we keep: those geonamescache knows a name for.
    codes = {code.lower(): info for code, info in gc_countries.items() if code}

    display: dict[str, str] = {}
    name_tokens: dict[str, set[str]] = {c: set() for c in codes}

    for code, info in codes.items():
        name = info.get("name") or code.upper()
        display[code] = name
        name_tokens[code].add(_ascii(name))
        # pycountry adds official/common variants (e.g. "Russian Federation").
        py = pycountry.countries.get(alpha_2=code.upper())
        if py:
            for attr in ("name", "official_name", "common_name"):
                v = getattr(py, attr, None)
                if v:
                    name_tokens[code].add(_ascii(v))
        for alias in MANUAL_ALIASES.get(code, []):
            name_tokens[code].add(_ascii(alias))
        cap = info.get("capital")
        if cap and len(_ascii(cap)) >= MIN_CITY_LEN:
            name_tokens[code].add(_ascii(cap))

    # Resolve each city NAME to a single country (highest population wins), so the
    # runtime data has no cross-country collisions (Manchester UK vs US, etc.).
    best_city: dict[str, tuple[int, str]] = {}   # ascii name -> (pop, cc)
    for c in gc_cities.values():
        pop = c.get("population") or 0
        if pop < CITY_POP_THRESHOLD:
            continue
        cc = (c.get("countrycode") or "").lower()
        if cc not in codes:
            continue
        nm = _ascii(c.get("name") or "")
        if len(nm.replace(" ", "")) < MIN_CITY_LEN or nm in CITY_STOPWORDS:
            continue
        cur = best_city.get(nm)
        if cur is None or pop > cur[0]:
            best_city[nm] = (pop, cc)

    city_tokens: dict[str, list[tuple[int, str]]] = {c: [] for c in codes}
    for nm, (pop, cc) in best_city.items():
        # Don't repeat a token that's already the country's own name.
        if nm in name_tokens[cc]:
            continue
        city_tokens[cc].append((pop, nm))

    cities_out: dict[str, list[str]] = {}
    fe_cities: dict[str, list[str]] = {}
    for cc, lst in city_tokens.items():
        lst.sort(key=lambda x: -x[0])
        cities_out[cc] = [nm for _p, nm in lst[:MAX_CITIES_PER_COUNTRY]]
        fe_cities[cc] = [nm for _p, nm in lst[:FE_CITIES_PER_COUNTRY]]

    return {
        "display": display,
        "name_tokens": {c: sorted(name_tokens[c]) for c in codes},
        "city_tokens": cities_out,
        "fe_cities": fe_cities,
    }


def _country_choices(display: dict[str, str]) -> list[tuple[str, str]]:
    # "global" sentinel first, then GB/US (primary markets), then the rest A-Z.
    rest = sorted((c for c in display if c not in ("gb", "us")),
                  key=lambda c: display[c].lower())
    ordered = ["gb", "us"] + rest
    choices = [("global", "Global (no filter)")]
    choices += [(c, display[c]) for c in ordered if c in display]
    return choices


def _adzuna_country_level(name_tokens: dict[str, list[str]]) -> list[str]:
    out: set[str] = set()
    for cc in ADZUNA_SUPPORTED:
        for t in name_tokens.get(cc, []):
            out.add(t)
    return sorted(out)


def write_python(data: dict) -> None:
    display = data["display"]
    name_tokens = data["name_tokens"]
    city_tokens = data["city_tokens"]

    merged = {c: sorted(set(name_tokens[c]) | set(city_tokens.get(c, [])))
              for c in display}

    # indent=0 gives one entry per line -> diff-friendly and valid Python literals.
    lines = [
        '"""AUTO-GENERATED by scripts/gen_countries.py -- do not edit by hand.',
        "",
        "Country reference data for the hard location filter and geo-targeting.",
        "Regenerate with:  venv/Scripts/python scripts/gen_countries.py",
        '"""',
        "",
        f"CC_DISPLAY = {json.dumps(display, indent=0, sort_keys=True)}",
        "",
        f"COUNTRY_NAME_TOKENS = {json.dumps(name_tokens, indent=0, sort_keys=True)}",
        "",
        f"CITY_TOKENS = {json.dumps(city_tokens, indent=0, sort_keys=True)}",
        "",
        f"COUNTRY_TOKENS = {json.dumps(merged, indent=0, sort_keys=True)}",
        "",
        f"ADZUNA_SUPPORTED = set({json.dumps(sorted(ADZUNA_SUPPORTED))})",
        "",
        f"ADZUNA_COUNTRY_LEVEL = set({json.dumps(_adzuna_country_level(name_tokens))})",
        "",
    ]
    (ROOT / "countries_data.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    choices = _country_choices(display)
    # Render as list-of-tuples to match the existing config.py shape.
    tuples = ",\n    ".join(f'("{c}", {json.dumps(lbl)})' for c, lbl in choices)
    py = [
        '"""AUTO-GENERATED by scripts/gen_countries.py -- do not edit by hand."""',
        "",
        "# code -> Adzuna cc / label; 'global' is the no-filter sentinel.",
        "COUNTRY_CHOICES = [",
        f"    {tuples},",
        "]",
        "",
    ]
    (ROOT / "backend" / "app" / "countries_gen.py").write_text(
        "\n".join(py), encoding="utf-8")


def write_frontend(data: dict) -> None:
    display = data["display"]
    name_tokens = data["name_tokens"]
    fe_cities = data["fe_cities"]
    choices = _country_choices(display)

    detect: dict[str, list[str]] = {}
    for cc in display:
        toks = sorted(set(name_tokens[cc]) | set(fe_cities.get(cc, [])))
        if toks:
            detect[cc] = toks

    ts = [
        "// AUTO-GENERATED by scripts/gen_countries.py -- do not edit by hand.",
        "",
        "export const COUNTRY_CHOICES: { code: string; label: string }[] = [",
    ]
    for c, lbl in choices:
        ts.append(f'  {{ code: {json.dumps(c)}, label: {json.dumps(lbl)} }},')
    ts.append("];")
    ts.append("")
    ts.append("// Country name/alias + a few major cities, for auto-highlighting the")
    ts.append("// inferred country chip in the picker. The dropdown covers the rest.")
    ts.append("export const COUNTRY_TOKENS: Record<string, string[]> = {")
    for cc in sorted(detect):
        arr = ", ".join(json.dumps(t) for t in detect[cc])
        ts.append(f'  {json.dumps(cc)}: [{arr}],')
    ts.append("};")
    ts.append("")
    (ROOT / "frontend" / "lib" / "countries.gen.ts").write_text(
        "\n".join(ts), encoding="utf-8")


def main() -> None:
    data = build()
    write_python(data)
    write_frontend(data)
    n = len(data["display"])
    print(f"Generated country data for {n} countries.")
    print("  countries_data.py, backend/app/countries_gen.py, "
          "frontend/lib/countries.gen.ts")


if __name__ == "__main__":
    main()
