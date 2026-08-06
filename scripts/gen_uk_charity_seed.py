"""Dev-only generator for the UK charity employer seed list.

Same posture as scripts/gen_uk_geo.py and scripts/gen_countries.py: one
dev-time source of truth, one generated plain-data module the app imports, zero
runtime dependencies and zero per-search API calls.

  source (gitignored, ~507MB):  txt non code/publicextract.charity.json
  generated (committed):        backend/app/uk_charity_gen.py

WHY THIS EXISTS
The ATS discovery tier is a registry of ~1,800 companies that is overwhelmingly
US-headquartered, and on a measured live run it produced 4 of 85 surfaced roles
(4.7%) while the term-based board APIs produced 79. The largest single entry in
the job store is one US aerospace company's board at 1,943 rows, almost all of
which the country filter then discards. More ATS VENDORS does not fix that --
vendor coverage is not what is missing, UK employer coverage is.

The Charity Commission's public register is the seed list for one underserved
UK vertical where the employers are nameable in advance: every registered
charity, with its own website. That is the hard half of a direct-employer
crawler, and it is already a solved, freely-licensed dataset.

WHAT IS FILTERED OUT AND WHY
  * charity_registration_status != "Registered"   -- removed/dissolved charities
  * linked_charity_number != 0                    -- linked subsidiary records
    duplicate their parent (the register carries ~398k rows for ~185k charities)
  * no charity_contact_web                        -- nothing to crawl
  * latest_income < MIN_INCOME                    -- the load-bearing one. Of
    171,658 registered charities, 103,155 have a website but only 8,437 report
    income over 1M. A charity with 50k of income has no payroll and therefore no
    vacancies page; crawling it is pure cost. Income is the only proxy in the
    dataset for "does this organisation employ anyone".

NOT CARRIED OVER: charity_activities, a free-text description of what the
charity does. It is genuinely useful signal for sector matching, but at ~200
chars x thousands of rows it would dominate the module. If it is ever wanted,
put it in its own generated module rather than widening this one.

Run:  venv/Scripts/python scripts/gen_uk_charity_seed.py
      venv/Scripts/python scripts/gen_uk_charity_seed.py --min-income 250000
Deps: none beyond the standard library.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.parse import urlsplit

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(BASE_DIR, "txt non code", "publicextract.charity.json")
OUT = os.path.join(BASE_DIR, "backend", "app", "uk_charity_gen.py")

# See the module docstring: below this, a charity has no payroll to hire onto.
DEFAULT_MIN_INCOME = 1_000_000

# Hosts that appear in charity_contact_web but are somebody else's site, so they
# have no vacancies page of their own to find. justgiving/localgiving are
# donation pages; the social networks are self-explanatory. Matched on the
# registrable host, so a subdomain of one of these is caught too.
_NOT_OWN_SITE = {
    "facebook.com", "www.facebook.com", "twitter.com", "x.com", "instagram.com",
    "linkedin.com", "youtube.com", "justgiving.com", "localgiving.org",
    "wixsite.com", "wordpress.com", "blogspot.com", "weebly.com", "google.com",
    "sites.google.com", "gofundme.com", "charitychoice.co.uk", "tumblr.com",
}

_HOST_OK_RE = re.compile(r"^[a-z0-9.-]+\.[a-z]{2,}$")


def normalise_domain(raw: str) -> str | None:
    """charity_contact_web -> a bare registrable host, or None if unusable.

    The field is free text typed by charity administrators, so it arrives in
    every shape: "www.example.org", "http://example.org/", "example.org/jobs",
    "E-mail: x@example.org", and plenty that is not a URL at all."""
    if not raw:
        return None
    value = raw.strip().strip('"\'<>').lower()
    if not value or " " in value.strip():
        # A space means free text ("see facebook page"), not a URL. Checked
        # before the scheme is stripped so "http://a b" is rejected too.
        value = value.split()[0] if value.split() else ""
    if not value:
        return None
    if "://" not in value:
        value = "http://" + value
    host = urlsplit(value).netloc
    if "@" in host:                      # an email address typed into the web field
        host = host.split("@", 1)[1]
    host = host.split(":", 1)[0]         # strip any port
    if host.startswith("www."):
        host = host[4:]
    if not host or not _HOST_OK_RE.match(host):
        return None
    if host in _NOT_OWN_SITE:
        return None
    # A subdomain of a site-builder / social host is still not the charity's own
    # crawlable site (e.g. "mycharity.wixsite.com").
    if any(host.endswith("." + bad) for bad in _NOT_OWN_SITE):
        return None
    return host


def iter_register(path: str):
    """Stream the register one record at a time.

    The extract is a JSON array formatted one object per line (`[{...}\\n,{...}`),
    so it can be read line-by-line. json.load() on the whole file would need
    several GB of RAM for a 507MB document, for no benefit."""
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("["):
                line = line[1:]
            if line.startswith(","):
                line = line[1:]
            if line.endswith("]"):
                line = line[:-1]
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def build(min_income: int) -> list[tuple]:
    by_domain: dict[str, tuple] = {}
    stats = {"rows": 0, "registered": 0, "main": 0, "income": 0, "web": 0}

    for rec in iter_register(SOURCE):
        stats["rows"] += 1
        if rec.get("charity_registration_status") != "Registered":
            continue
        stats["registered"] += 1
        if rec.get("linked_charity_number") not in (0, None):
            continue
        stats["main"] += 1
        income = rec.get("latest_income") or 0
        if income < min_income:
            continue
        stats["income"] += 1
        domain = normalise_domain(rec.get("charity_contact_web") or "")
        if not domain:
            continue
        stats["web"] += 1

        name = " ".join((rec.get("charity_name") or "").split())
        number = rec.get("registered_charity_number") or 0
        postcode = (rec.get("charity_contact_postcode") or "").strip().upper()
        row = (name, domain, int(income), int(number), postcode)
        # Several charities legitimately share one domain (a federation and its
        # trading arm). Keep the highest-income one: the crawl works per domain,
        # so a second row would only re-fetch the same pages.
        prev = by_domain.get(domain)
        if prev is None or income > prev[2]:
            by_domain[domain] = row

    print(f"  scanned {stats['rows']:,} records")
    print(f"  registered:            {stats['registered']:,}")
    print(f"  main (non-linked):     {stats['main']:,}")
    print(f"  income >= {min_income:,}: {stats['income']:,}")
    print(f"  with a usable website: {stats['web']:,}")
    print(f"  unique domains:        {len(by_domain):,}")
    # Income-descending: the crawler spends a bounded budget per pass, and a
    # bigger charity is likelier to have both a vacancies page and open roles.
    return sorted(by_domain.values(), key=lambda r: -r[2])


def write_module(rows: list[tuple], min_income: int) -> None:
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write('"""UK charity employer seed list -- GENERATED, do not edit by hand.\n\n')
        fh.write("Regenerate with:  venv/Scripts/python scripts/gen_uk_charity_seed.py\n")
        fh.write("Source: Charity Commission public register (publicextract.charity.json),\n")
        fh.write("Open Government Licence v3.0.\n\n")
        fh.write(f"Rows are (name, domain, latest_income, charity_number, postcode),\n")
        fh.write(f"income-descending, one per domain, filtered to registered charities\n")
        fh.write(f"reporting at least {min_income:,} of annual income with a usable website.\n")
        fh.write('"""\n\n')
        fh.write(f"MIN_INCOME = {min_income}\n\n")
        fh.write("CHARITIES = [\n")
        for name, domain, income, number, postcode in rows:
            fh.write(f"    ({name!r}, {domain!r}, {income}, {number}, {postcode!r}),\n")
        fh.write("]\n")
    print(f"  wrote {OUT} ({os.path.getsize(OUT) / 1024:.0f} KB)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-income", type=int, default=DEFAULT_MIN_INCOME,
                    help=f"minimum latest_income (default {DEFAULT_MIN_INCOME:,})")
    args = ap.parse_args()

    if not os.path.exists(SOURCE):
        print(f"Source not found: {SOURCE}\n"
              "Download the 'Charity' extract from\n"
              "  https://register-of-charities.charitycommission.gov.uk/register/full-register-download",
              file=sys.stderr)
        return 1

    print(f"Reading {SOURCE} …")
    rows = build(args.min_income)
    if not rows:
        print("No rows produced -- refusing to overwrite the module.", file=sys.stderr)
        return 1
    write_module(rows, args.min_income)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
