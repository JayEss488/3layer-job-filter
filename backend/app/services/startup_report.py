"""What this install is actually configured to do, printed once at boot.

Every job board in this app fails SOFT: a source with no API key returns an
empty list and the run carries on with whatever else is configured (see
full_auto.py -- each fetcher's first line is a key check). That is the right
behaviour, and it has one bad property: a missing key is completely silent. A
run with three of five sources unconfigured looks exactly like a run that found
less than usual, and the only evidence is a number that nobody has a baseline
for.

The AI key fails differently and worse. Its absence -- or a model name the key
cannot reach -- surfaces inside a pipeline stage that is deliberately fail-open,
so the run COMPLETES, returns little or nothing, and logs a JSON parse error
rather than an authentication error. That failure is expensive to diagnose and
trivial to prevent by printing the routing before a search is ever spent on it.

Read-only: this prints, it never validates a key over the network and never
changes behaviour.
"""
import os

# (env var, label, what you lose without it). Ordered by how much a UK user
# actually loses, which is also the order the README recommends getting them in.
_SOURCES = [
    ("REED_API_KEY", "Reed", "UK's largest board -- free key, the single biggest win"),
    ("ADZUNA_APP_ID", "Adzuna", "broad aggregator, 20+ countries (needs ADZUNA_APP_KEY too)"),
    ("RAPIDAPI_KEY", "JSearch", "Google-for-Jobs mirror via RapidAPI"),
    ("SERPER_DEV_API_KEY", "Google organic", "finds postings on company sites and smaller boards"),
    ("SERPAPI_KEY", "SerpAPI", "alternative Google Jobs provider (SERPER is preferred)"),
    ("USAJOBS_API_KEY", "USAJOBS", "US federal roles only (needs USAJOBS_USER_AGENT too)"),
]


def _tier_models() -> dict[str, str]:
    """The model configured at each tier, read the same way the callers read it.

    Duplicating the getenv defaults rather than importing them is deliberate:
    full_auto.py pulls in crawl4ai/playwright at import time, and this runs
    during startup where that import is specifically avoided. The three names
    are checked against their real homes by test-free inspection only, so keep
    them in sync with full_auto.py and services/llm.py if a default changes.
    """
    return {
        "cheap": os.getenv("ENGINE_CHEAP_MODEL", "gpt-5.4-nano-2026-03-17"),
        "mid": os.getenv("ENGINE_MID_MODEL", "gpt-5.6-luna"),
        "judge": os.getenv("ENGINE_EXP_MODEL", "gpt-5.6-terra"),
        "embedding": os.getenv("ENGINE_EMBED_MODEL", "text-embedding-3-small"),
    }


def report() -> None:
    """Print the AI routing and the configured job sources. Never raises."""
    try:
        import llm_providers
    except Exception as e:  # pragma: no cover - the module is a leaf import
        print(f"[startup] could not load the provider layer: {e!r}")
        return

    print("[startup] AI models")
    models = _tier_models()
    missing_keys: set[str] = set()
    for tier, model in models.items():
        if tier == "embedding":
            prov = "voyage" if llm_providers.EMBEDDING_PROVIDER == "voyage" else "openai"
            key = "VOYAGE_API_KEY" if prov == "voyage" else "OPENAI_API_KEY"
        else:
            prov = llm_providers.provider_for(model)
            key = "ANTHROPIC_API_KEY" if prov == "anthropic" else "OPENAI_API_KEY"
        have = bool(os.getenv(key))
        if not have:
            missing_keys.add(key)
        print(f"[startup]   {tier:<10} {model:<28} {prov:<9} {key}: {'set' if have else 'MISSING'}")

    if missing_keys:
        # Loud, and the only thing here that is: with no usable model key the
        # app still starts, still serves the UI and still accepts a search --
        # which then produces nothing, slowly, for reasons that look like a
        # ranking problem rather than a configuration one.
        print(f"[startup]   !! {', '.join(sorted(missing_keys))} not set. A search will "
              "return nothing until you add it to .env.")
        print("[startup]   !! If your key cannot reach the model names above, set "
              "ENGINE_CHEAP_MODEL / ENGINE_MID_MODEL / ENGINE_EXP_MODEL to ones it can.")

    configured = [(label, why) for var, label, why in _SOURCES if os.getenv(var)]
    unconfigured = [(label, why) for var, label, why in _SOURCES if not os.getenv(var)]
    print(f"[startup] job sources: {len(configured)} configured"
          + (f" -- {', '.join(l for l, _ in configured)}" if configured else ""))
    for label, why in unconfigured:
        print(f"[startup]   off: {label:<16} ({why})")
    if not configured:
        # Not fatal and deliberately not an error: the ATS vendor boards
        # (Greenhouse, Lever, Ashby, ...) need no key at all and still run, so
        # this install works -- just far more narrowly than it could.
        print("[startup]   no board API keys set -- discovery falls back to the "
              "no-key ATS company boards only. Reed and Adzuna are free; see the README.")
