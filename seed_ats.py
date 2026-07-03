#!/usr/bin/env python3
"""Seed the company_ats token store used by the ATS discovery tier.

Two ways to populate, used together by default:

  1. A curated candidate list of well-known public ATS boards. Each candidate is
     VALIDATED live (we actually call the feed) and only kept if it returns at
     least one open role, so stale/wrong guesses are silently skipped - the store
     never accumulates dead tokens.

  2. Optional SerpAPI harvest (--harvest "<kw1>" "<kw2>" ...): site: searches for
     more boards in your sectors. Costs SerpAPI credits; run occasionally.

Usage:
    ../venv/Scripts/python seed_ats.py
    ../venv/Scripts/python seed_ats.py --harvest "climate" "fintech" "data engineering"

Idempotent: company_ats has a UNIQUE(vendor, token) constraint, so re-running
only adds newly-validated boards.
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import full_auto as engine

# Curated candidates: companies with public ATS boards, chosen to cover the
# actual target sectors (marketing/comms, ops/analyst, media/publishing,
# nonprofits, remote-first UK/EU, scale-ups) rather than only FAANG-adjacent
# engineering shops -- those over-index the pool with irrelevant roles. Generous
# on purpose: live validation below drops any token that's wrong or has moved,
# so the store never accumulates dead boards. The SerpAPI harvest (--harvest,
# fed profile-derived keywords) is what grows this beyond the curated seed.
CANDIDATES = {
    "greenhouse": [
        # engineering / product tech (kept so software/data profiles stay covered)
        "databricks", "figma", "cloudflare", "datadog", "mongodb", "snowflake",
        "confluent", "discord", "reddit", "pinterest", "dropbox", "samsara",
        "benchling", "coinbase", "plaid", "robinhood", "stripe", "hashicorp",
        "elastic", "gitlab", "airtable", "twilio",
        # media / publishing / comms (marketing, editorial, outreach, analyst roles)
        "voxmedia", "buzzfeed", "npr", "theguardian", "guardiannewsandmedia",
        "condenast", "theathletic", "vice", "dotdashmeredith", "gannett",
        # nonprofits / mission-driven (ops, comms, programme, data roles)
        "wikimedia", "codeforamerica", "khanacademy", "girlswhocode", "mozilla",
        "wikimediafoundation", "chanzuckerberg", "propublica", "malala",
        # remote-first / distributed orgs (broad non-eng hiring)
        "remotecom", "oysterhr", "close", "hopin", "gohenry", "hotjar",
        "typeform", "personio",
        # UK/EU scale-ups & ops-heavy consumer businesses (marketing, ops, analyst)
        "deliveroo", "gousto", "depop", "trainline", "octoenergy", "starlingbank",
        "gocardless", "monzo", "wise", "tide", "sumup", "cazoo", "onfido",
        "moneybox", "freetrade", "cleo", "zego", "bulb", "farfetch", "vinted",
        # broad hirers (large marketing/ops/data orgs alongside eng)
        "gusto", "lattice", "asana",
    ],
    "lever": [
        # media / consumer / marketplaces with heavy ops & marketing hiring
        "netflix", "spotify", "yelp", "match", "nubank",
        # engineering / fintech (kept for software/data profiles)
        "ramp", "plaid", "kraken",
        # agencies / marketing / creative / remote-first
        "buffer", "canva", "brandwatch", "huel", "brew", "shopify",
        # nonprofits / social impact
        "leadgenius", "kiva", "watershed",
    ],
    "ashby": [
        # AI / engineering labs (kept so technical profiles stay covered)
        "openai", "cohere", "mistral", "perplexity", "huggingface", "together",
        "anysphere", "scaleai", "replicate", "modal", "runway", "elevenlabs",
        # remote-first scale-ups that hire broadly across ops/marketing/comms
        "deel", "notion", "linear", "vanta", "posthog", "clay", "ramp",
        "remote", "gumroad", "loom", "webflow", "mercury", "zapier",
    ],
    # Newer vendors (see ATS_FEEDS): a small confident seed only -- the SerpAPI
    # harvest is the real driver of coverage here, since these skew to many
    # smaller EU/remote-first orgs whose tokens aren't well-known.
    "workable": [
        "hotjar", "typeform", "remote",
    ],
    "recruitee": [
        "recruitee",
    ],
    "personio": [
        "personio",
    ],
}


def validate(vendor: str, token: str):
    """Return (vendor, token, n_jobs) if the feed returns >=1 role, else None."""
    try:
        jobs = engine.fetch_ats(vendor, token)
    except Exception:
        return None
    return (vendor, token, len(jobs)) if jobs else None


def seed_curated() -> list[tuple]:
    pairs = [(v, t) for v, tokens in CANDIDATES.items() for t in tokens]
    print(f"[seed] Validating {len(pairs)} candidate ATS boards...")
    live = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(validate, v, t): (v, t) for v, t in pairs}
        for fut in as_completed(futs):
            res = fut.result()
            if res:
                vendor, token, n = res
                live.append((token, vendor, token))  # (company, vendor, token)
                print(f"   [OK] {vendor:10} {token:18} {n} roles")
    engine.save_company_ats(live)
    print(f"[seed] Saved {len(live)} validated boards to company_ats.")
    return live


def main():
    args = sys.argv[1:]
    seed_curated()

    if args and args[0] == "--harvest":
        keywords = args[1:] or ["technology"]
        print(f"[seed] SerpAPI harvest for: {', '.join(keywords)}")
        engine.harvest_ats_tokens(keywords)

    total = len(engine.load_company_ats())
    print(f"[seed] company_ats now holds {total} tokens total.")


if __name__ == "__main__":
    main()
