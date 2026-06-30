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

# Curated candidates: well-known companies with public job boards. Generous on
# purpose - live validation below filters out any that are wrong or have moved.
CANDIDATES = {
    "greenhouse": [
        "stripe", "databricks", "robinhood", "coinbase", "dropbox", "instacart",
        "brex", "plaid", "figma", "retool", "discord", "cloudflare", "doordash",
        "lyft", "pinterest", "reddit", "asana", "benchling", "samsara", "affirm",
        "chime", "gusto", "lattice", "mongodb", "hashicorp", "elastic",
        "confluent", "snowflake", "datadog", "gitlab", "airtable", "twilio",
        "sofi", "wise", "monzo", "deliveroo", "gocardless", "starlingbank",
        "improbable", "wayve", "octoenergy", "revolut", "palantir",
    ],
    "lever": [
        "netflix", "spotify", "match", "yelp", "leadgenius", "lever", "ramp",
        "plaid", "nubank", "kraken", "blockchain", "voiceflow",
    ],
    "ashby": [
        "openai", "ramp", "linear", "vanta", "posthog", "replicate", "modal",
        "cohere", "mistral", "deel", "runway", "elevenlabs", "perplexity",
        "huggingface", "together", "anysphere", "notion", "scaleai", "clay",
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
