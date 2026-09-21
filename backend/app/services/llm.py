"""Thin LLM wrapper shared by parsing + suggestion services.

Kept independent of the search engine (full_auto.py) so the app's lightweight LLM
calls don't drag in crawl4ai/playwright at import time.

Provider-agnostic: the actual call goes through llm_providers.py (repo root),
which routes on the model NAME -- so setting MID_MODEL to a Claude model moves
CV parsing to Anthropic with no change here. The three tier constants below are
deliberately SEPARATE from full_auto.py's ENGINE_-prefixed ones so that an A/B
on the search pipeline doesn't silently retune CV parsing too."""
import contextvars
import json
import os
import time
from contextlib import contextmanager

import llm_providers

# Match the model family the engine uses for cheap calls. Deliberately stays on
# the older/cheaper nano tier (matches full_auto.py's CHEAP_MODEL) rather than
# GPT-5.6 Luna -- nano is meaningfully cheaper and plenty for these calls.
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "gpt-5.4-nano-2026-03-17")
# Middle tier (GPT-5.6 Luna, matches full_auto.py's MID_MODEL). The profile-
# formation calls (CV extraction, the "understand" summary/header/role-family
# call) run here rather than on STRONG_MODEL: at ~4400 words a CV parse was
# taking 23s/call on the strong tier, and Luna gives most of the reasoning
# quality at a fraction of the latency/cost -- and those calls now run two at a
# time in parallel, so the strong tier's extra seconds hurt twice over.
MID_MODEL = os.getenv("MID_MODEL", "gpt-5.6-luna")
# Reserved for low-frequency, high-value calls where reasoning quality matters
# more than per-call cost. Matches full_auto.py's EXP_MODEL (GPT-5.6 Terra);
# kept as a plain string here so this module stays independent of
# full_auto/crawl4ai (see module docstring).
STRONG_MODEL = os.getenv("STRONG_MODEL", "gpt-5.6-terra")


# Per-context trace sink for llm_json calls. None = tracing off (the default,
# so every existing call site is untouched and pays nothing). The CV-parse
# timing diagnostic (services/diagnostics.py) sets this via capture_llm_calls()
# to attribute latency/tokens to individual model calls without threading a
# collector through every function in the parse pipeline.
_llm_trace: contextvars.ContextVar[list | None] = contextvars.ContextVar("_llm_trace", default=None)


@contextmanager
def capture_llm_calls():
    """Collect a trace ({model, prompt_chars, duration_s, attempts, ok, tokens})
    of every llm_json call made in this context. Set once per stage by the timing
    diagnostic; a no-op for all other callers. Not designed to nest -- an inner
    capture shadows the outer for its own scope (fine, since the diagnostic wraps
    each stage separately rather than nesting)."""
    calls: list[dict] = []
    token = _llm_trace.set(calls)
    try:
        yield calls
    finally:
        _llm_trace.reset(token)


# Per-attempt read timeout, and how many attempts the SDK makes underneath
# llm_json's own retry loop. BOTH have to be set explicitly, because the two
# retry layers MULTIPLY and only one of them was ever visible from this file:
# llm_json retries once, the SDK defaulted to max_retries=2 (three attempts),
# and at the old 90s timeout that is 6 x 90s = ~9 minutes of a user-facing CV
# upload spent inside a call that has already stalled, before anything is
# raised or logged. That is what an "it just span forever, no console errors"
# report looks like from the inside.
#
# 45s is chosen against the measured distribution rather than as a round number:
# every caller here is a short, interactive, one-shot call (CV extraction, the
# summary/families pair, suggestions, family assignment, region inference), and
# the longest of them -- the ~450-word summary on a 4,400-word CV -- measures
# ~11s. 45s is four times the slowest normal case, so a legitimate slow day is
# never cut off, while a genuine stall now fails in time for llm_json's retry to
# actually be worth having. Worst case is 4 x 45s = 180s, bounded above it by
# config.CV_PARSE_TIMEOUT_SECONDS on the path where a human is waiting.
#
# Do NOT reuse these for the search engine's calls -- full_auto.py keeps its own
# client at 90s because the final judge legitimately generates thousands of
# output tokens per call.
_READ_TIMEOUT_SECONDS = 45.0
_SDK_MAX_RETRIES = 1


def _clean_json(raw: str) -> str:
    return (
        raw.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )


# Some OpenAI tiers (gpt-5.5, gpt-5.6-*) reject any non-default temperature
# outright (400 Unsupported value) -- mirrors full_auto.py's llm()/EXP_MODEL
# handling, kept duplicated rather than shared since this module deliberately
# stays independent of full_auto.py (see module docstring). Confirmed via a live
# 400 that silently emptied every CV parse for months: llm_json swallowed the
# exception below and returned {}, which looked identical to "the model found
# nothing on this CV".
#
# A PREFIX test, not exact membership, for the same reason full_auto.py uses one:
# the model names are env-overridable, so a perfectly reasonable dated variant
# ("gpt-5.6-luna-2026-05-01") would miss an exact tuple and get the 400.
_FIXED_TEMPERATURE_PREFIXES = ("gpt-5.5", "gpt-5.6")


def _temperature_for(model: str) -> float:
    """The temperature to send for `model`.

    Only OpenAI has the fixed-temperature restriction above; Anthropic accepts
    the full 0..1 range on every model, so the restriction must not be applied
    to a Claude model that happens to be configured here."""
    if llm_providers.provider_for(model) != "openai":
        return 0.2
    return 1 if any((model or "").startswith(p) for p in _FIXED_TEMPERATURE_PREFIXES) else 0.2


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
    temperature = _temperature_for(model)

    trace = _llm_trace.get()
    started = time.perf_counter()
    result: dict = {}
    usage = None
    ok = False
    attempts_used = 0
    for attempt in (1, 2):
        attempts_used = attempt
        try:
            resp = llm_providers.chat(
                prompt,
                system=system,
                model=model,
                require_json=True,
                temperature=temperature,
                timeout=_READ_TIMEOUT_SECONDS,
                max_retries=_SDK_MAX_RETRIES,
            )
            result = json.loads(_clean_json(resp.text))
            usage = resp.usage
            ok = True
            break
        except Exception as e:
            if attempt == 2:
                print(f"[llm_json] call failed (model={model}): {e}")
                break
            status = getattr(e, "status_code", None)
            resp_obj = getattr(e, "response", None)
            retry_after = resp_obj.headers.get("retry-after") if resp_obj is not None else None
            try:
                wait = float(retry_after) if retry_after else 2.0
            except (TypeError, ValueError):
                wait = 2.0
            print(f"[llm_json] {model} call failed (status={status}): {e}; retrying after {wait}s.")
            time.sleep(wait)

    if trace is not None:
        trace.append({
            "model": model,
            "prompt_chars": len(prompt) + len(system or ""),
            "duration_s": round(time.perf_counter() - started, 3),
            "attempts": attempts_used,
            "ok": ok,
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        })
    return result
