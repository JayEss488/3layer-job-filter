"""Drift guard for the generated country data (run manually; no pytest in repo).

Asserts the three generated artifacts are internally consistent, so a hand-edit
can't reintroduce the "selectable but unfilterable" mismatch that the
country-coverage work fixed: every country offered in the dropdown must have
filter tokens the engine can positively match on.

Run:  venv/Scripts/python tests/check_country_data.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import countries_data as cd
from app.countries_gen import COUNTRY_CHOICES

errors = []

for code, label in COUNTRY_CHOICES:
    if code == "global":
        continue
    if code not in cd.CC_DISPLAY:
        errors.append(f"{code} ({label}) missing from CC_DISPLAY")
    if not cd.COUNTRY_TOKENS.get(code):
        errors.append(f"{code} ({label}) has no filter tokens -> selectable but unfilterable")

# Every Adzuna-supported cc should also be a valid country we can display/filter.
for cc in cd.ADZUNA_SUPPORTED:
    if cc not in cd.CC_DISPLAY:
        errors.append(f"adzuna-supported {cc} missing from CC_DISPLAY")

if errors:
    print("COUNTRY DATA DRIFT DETECTED:")
    for e in errors:
        print("  -", e)
    print("\nRegenerate with: venv/Scripts/python scripts/gen_countries.py")
    sys.exit(1)

print(f"OK: {len(COUNTRY_CHOICES)-1} selectable countries, all with display "
      f"names and filter tokens; {len(cd.ADZUNA_SUPPORTED)} Adzuna nodes valid.")
