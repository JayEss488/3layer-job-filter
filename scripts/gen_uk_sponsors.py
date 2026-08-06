"""Dev-only generator for the UK licensed visa-sponsor name list.

Same posture as scripts/gen_uk_charity_seed.py, scripts/gen_uk_geo.py and
scripts/gen_countries.py: one dev-time source of truth, one generated plain-data
module the app imports, zero runtime dependencies and zero per-search API calls.

  source (gitignored):   txt non code/SP_-_Worker_and_Temporary_Worker_Web_Register_-_*.csv
  generated (committed): backend/app/uk_sponsor_gen.py

The source CSV lives under the gitignored /txt non code/, so the generated
module is what ships -- exactly as for the Charity Commission extract.

WHAT THE REGISTER IS
The Home Office publishes every organisation licensed to sponsor a Worker or
Temporary Worker visa, refreshed regularly. Columns are Organisation Name,
Town/City, County, Type & Rating, Route. There are no domains, no company
numbers and no sector, so the only join key available to this app is the
organisation NAME -- see backend/app/services/sponsors.py for what that can and
cannot tell you, and for the measured match rate.

WHAT IS STORED
Only the normalised name key (services.sponsors.normalise -> key), one per line,
deduped. Not the town, not the rating, not the route:

  * The matcher keys on name alone, so the rest would be dead weight in a module
    that is already ~2MB.
  * Town/City would be a tempting disambiguator for the ~1,500 names that repeat
    across the register, but a job listing's location is the VACANCY's location,
    not the licence holder's registered office, so matching them would be wrong
    more often than right.

Stored as ONE newline-joined string literal rather than 127k separate literals:
the parse cost of a single large literal is a fraction of the per-literal cost,
and uk_sponsor_gen is imported lazily anyway.

ROUTES
All routes are kept by default. Filtering to Skilled Worker alone would leave
121,891 of 127,232 unique names -- a rounding error -- while risking the removal
of the Charity Worker route, which is exactly the vertical this app's charity
employer crawl targets. --routes is there if that ever changes.

Run:  venv/Scripts/python scripts/gen_uk_sponsors.py
      venv/Scripts/python scripts/gen_uk_sponsors.py --routes "Skilled Worker"
Deps: none beyond the standard library.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The Home Office puts the publication date in the filename, so glob rather than
# hardcode it: a refreshed register is a new file, not an edited one.
SOURCE_GLOB = os.path.join(BASE_DIR, "txt non code",
                           "SP_-_Worker_and_Temporary_Worker_Web_Register_-_*.csv")
OUT = os.path.join(BASE_DIR, "backend", "app", "uk_sponsor_gen.py")

sys.path.insert(0, os.path.join(BASE_DIR, "backend"))
from app.services import sponsors  # noqa: E402  (after sys.path setup)


def newest_source() -> str | None:
    matches = sorted(glob.glob(SOURCE_GLOB))
    return matches[-1] if matches else None


def build(path: str, routes: set[str] | None) -> tuple[list[str], dict]:
    keys: set[str] = set()
    stats = {"rows": 0, "route_kept": 0, "named": 0, "unique_names": set()}

    with open(path, encoding="utf-8-sig", newline="") as fh:
        for rec in csv.DictReader(fh):
            stats["rows"] += 1
            if routes and (rec.get("Route") or "").strip() not in routes:
                continue
            stats["route_kept"] += 1
            name = " ".join((rec.get("Organisation Name") or "").split())
            if not name:
                continue
            stats["named"] += 1
            stats["unique_names"].add(name)
            for tokens in sponsors.variants(name):
                k = sponsors.key(tokens)
                if k:
                    keys.add(k)

    stats["unique_names"] = len(stats["unique_names"])
    print(f"  scanned {stats['rows']:,} rows")
    if routes:
        print(f"  matching route filter:  {stats['route_kept']:,}")
    print(f"  unique organisations:   {stats['unique_names']:,}")
    print(f"  normalised keys:        {len(keys):,}  (incl. trading-as aliases)")
    return sorted(keys), stats


def write_module(keys: list[str], source: str, routes: set[str] | None,
                 unique_names: int) -> None:
    route_note = ", ".join(sorted(routes)) if routes else "all routes"
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write('"""UK licensed visa sponsors -- GENERATED, do not edit by hand.\n\n')
        fh.write("Regenerate with:  venv/Scripts/python scripts/gen_uk_sponsors.py\n")
        fh.write("Source: Home Office 'Register of licensed sponsors: workers',\n")
        fh.write("Open Government Licence v3.0.\n\n")
        fh.write(f"Source file: {os.path.basename(source)}\n")
        fh.write(f"Routes kept: {route_note}\n")
        fh.write(f"Unique organisations: {unique_names:,}\n")
        fh.write(f"Normalised keys (incl. trading-as aliases): {len(keys):,}\n\n")
        fh.write("Keys are services.sponsors.normalise() output, space-joined, one per\n")
        fh.write("line inside a single string literal -- see the generator's docstring for\n")
        fh.write("why one literal rather than a list of them. Read them through\n")
        fh.write("sponsor_keys(), never by touching _KEYS_RAW.\n")
        fh.write('"""\n\n')
        fh.write(f"GENERATED_AT = {dt.date.today().isoformat()!r}\n")
        fh.write(f"SOURCE_FILE = {os.path.basename(source)!r}\n")
        fh.write(f"ROUTES = {sorted(routes) if routes else None!r}\n")
        fh.write(f"UNIQUE_ORGANISATIONS = {unique_names}\n")
        fh.write(f"SPONSOR_COUNT = {len(keys)}\n\n")
        fh.write('_KEYS_RAW = """\\\n')
        fh.write("\n".join(keys))
        fh.write('"""\n\n')
        fh.write("_KEYS: tuple = ()\n\n\n")
        fh.write("def sponsor_keys() -> tuple:\n")
        fh.write('    """The normalised keys, split once and memoised."""\n')
        fh.write("    global _KEYS\n")
        fh.write("    if not _KEYS:\n")
        fh.write('        _KEYS = tuple(_KEYS_RAW.split("\\n"))\n')
        fh.write("    return _KEYS\n")
    print(f"  wrote {OUT} ({os.path.getsize(OUT) / 1024 / 1024:.1f} MB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--routes", nargs="*", default=None,
                    help="only keep these Route values (default: all routes)")
    ap.add_argument("--source", default=None, help="explicit CSV path")
    args = ap.parse_args()

    source = args.source or newest_source()
    if not source or not os.path.exists(source):
        print(f"Source not found: {SOURCE_GLOB}\n"
              "Download 'Register of licensed sponsors: workers' (CSV) from\n"
              "  https://www.gov.uk/government/publications/register-of-licensed-sponsors-workers",
              file=sys.stderr)
        return 1

    print(f"Reading {source} …")
    routes = set(args.routes) if args.routes else None
    keys, stats = build(source, routes)
    if not keys:
        print("No keys produced -- refusing to overwrite the module.", file=sys.stderr)
        return 1
    write_module(keys, source, routes, stats["unique_names"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
