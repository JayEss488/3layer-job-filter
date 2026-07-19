"""Thin OpenAI wrapper shared by parsing + suggestion services.

Kept independent of the search engine (full_auto.py) so the app's lightweight LLM
calls don't drag in crawl4ai/playwright at import time."""
import json
import os
import time
from functools import lru_cache

import httpx
from openai import OpenAI

# Match the model family the engine uses for cheap calls. Deliberately stays on
# the older/cheaper nano tier (matches full_auto.py's CHEAP_MODEL) rather than
# GPT-5.6 Luna -- nano is meaningfully cheaper and plenty for these calls.
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "gpt-5.4-nano-2026-03-17")
# Reserved for low-frequency, high-value calls (CV parsing, target-role
# suggestions) where reasoning quality matters more than per-call cost.
# Matches full_auto.py's EXP_MODEL (GPT-5.6 Terra); kept as a plain string here
# so this module stays independent of full_auto/crawl4ai (see module docstring).
STRONG_MODEL = os.getenv("STRONG_MODEL", "gpt-5.6-terra")


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    # Same fix as full_auto.py's client: an explicit timeout instead of the SDK's
    # 600s default, so a stalled call fails fast instead of hanging the search
    # background task. This client's calls run first in a search (region
    # inference/role clustering, before full_auto is even imported), so a hang
    # here used to happen before anything else in the pipeline even started.
    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), timeout=httpx.Timeout(90.0, connect=5.0))


def _clean_json(raw: str) -> str:
    return (
        raw.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )


# Some tiers (gpt-5.5, gpt-5.6-terra) reject any non-default temperature outright
# (400 Unsupported value) -- mirrors full_auto.py's llm()/EXP_MODEL handling, kept
# duplicated rather than shared since this module deliberately stays independent
# of full_auto.py (see module docstring). Confirmed via a live 400 that silently
# emptied every CV parse for months: llm_json swallowed the exception below and
# returned {}, which looked identical to "the model found nothing on this CV".
_FIXED_TEMPERATURE_MODELS = ("gpt-5.5", "gpt-5.6-terra")


def llm_json(prompt: str, system: str = "", model: str = CHEAP_MODEL) -> dict:
    """Call the model and parse a JSON object out of the reply. Returns {} on failure
    (logged to stdout so a failure is at least visible in the server console instead
    of being indistinguishable from a genuine "nothing found" response).

    Retries once on the same model after a short pause -- mirrors
    full_auto.py's rank_gate handling of MID_MODEL/EXP_MODEL: a "permission"-flavored
    error that clears on an immediate retry is a burst/short-window cap, not a real
    per-key model restriction, and this module's calls (CV parsing, suggestions) are
    one-shot and user-facing, so a single transient hiccup shouldn't surface as a
    hard failure. Confirmed live: an identical STRONG_MODEL call 401'd, then
    succeeded seconds later with no code or key change."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    temperature = 1 if model in _FIXED_TEMPERATURE_MODELS else 0.2
    for attempt in (1, 2):
        try:
            resp = _client().chat.completions.create(
                model=model,
                messages=msgs,
                temperature=temperature,
                response_format={"type": "json_object"},
            )
            return json.loads(_clean_json(resp.choices[0].message.content))
        except Exception as e:
            if attempt == 2:
                print(f"[llm_json] call failed (model={model}): {e}")
                return {}
            status = getattr(e, "status_code", None)
            resp_obj = getattr(e, "response", None)
            retry_after = resp_obj.headers.get("retry-after") if resp_obj is not None else None
            try:
                wait = float(retry_after) if retry_after else 2.0
            except (TypeError, ValueError):
                wait = 2.0
            print(f"[llm_json] {model} call failed (status={status}): {e}; retrying after {wait}s.")
            time.sleep(wait)
