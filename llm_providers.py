"""Provider-agnostic chat + embedding calls. OpenAI and Anthropic, natively.

WHY THIS MODULE EXISTS
----------------------
The app spends money at three model tiers (cheap screen / mid rank / expensive
judge -- see README), and which model runs at each tier is the single most
useful knob a new user has. Before this, every call went through the OpenAI SDK,
so "configurable" could only ever mean "which OpenAI model".

There are exactly three call sites in the whole app -- ``full_auto.llm()``,
``full_auto.get_embeddings_batch()`` and ``backend/app/services/llm.py``'s
``llm_json()`` -- which is what makes one thin shim viable instead of a
framework.

WHERE IT SITS
-------------
Repo root, importing nothing from this project. Both callers can reach it:
``full_auto.py`` is a root module, and ``backend/app/config.py`` puts the repo
root on ``sys.path``. It deliberately does NOT live under ``backend/`` --
``services/llm.py`` stays independent of the engine so the app's lightweight
calls don't drag in crawl4ai/playwright at import time, and a shared module
under ``backend/`` would re-create that coupling in the other direction.

HOW A PROVIDER IS CHOSEN
------------------------
From the MODEL NAME, not from a global switch::

    ENGINE_EXP_MODEL=claude-opus-5     -> Anthropic
    ENGINE_CHEAP_MODEL=gpt-5.4-nano    -> OpenAI

so a user can run the cheap tier on one vendor and the judge on another by
setting two env vars, with nothing else to keep in sync. ``LLM_PROVIDER``
overrides the inference wholesale for anything unrecognised (a proxy, a local
server, a fine-tune with a custom name).

SDKs ARE IMPORTED LAZILY. Installing ``anthropic`` is optional: a user who runs
on OpenAI never needs it, and vice versa. An absent SDK raises only if a model
actually routed to it -- never at import.

WHAT IS NOT PORTABLE, AND IS HANDLED HERE
-----------------------------------------
* **System prompts.** OpenAI takes a system *message*; Anthropic takes a
  separate top-level ``system`` parameter.
* **JSON mode.** OpenAI has ``response_format``. Anthropic has no equivalent, so
  we prefill the assistant turn with ``{`` -- which constrains the model to
  continue a JSON object -- and glue the brace back on. Both callers already
  tolerate fenced/:dirty JSON, so this needs no change downstream.
* **max_tokens.** Optional on OpenAI, REQUIRED on Anthropic. ``ANTHROPIC_MAX_TOKENS``
  is the default when a caller passes none.
* **Usage accounting.** Anthropic reports ``input_tokens``/``output_tokens``/
  ``cache_read_input_tokens``. We re-shape it into the OpenAI field names
  ``full_auto._record_llm_usage`` already reads, so the per-stage token panel
  works identically on either provider.
* **Finish reason.** Anthropic's ``max_tokens`` is mapped to OpenAI's
  ``"length"``, because ``length_capped`` is the counter that distinguishes "the
  model chose to write less" from "we cut it off" -- a distinction the judge's
  checklist work turns on.
* **Prompt caching.** OpenAI's is automatic and its ``prompt_cache_key`` is a
  routing hint. Anthropic's is explicit: a ``cache_control`` marker on the
  system block. We set that marker whenever a caller asked for caching at all,
  so the judge's ~12k-token fixed prefix is cached on both providers.

EMBEDDINGS
----------
Anthropic has no embeddings API, and the embedding pre-filter is not optional in
this pipeline. So the embedding provider is configured SEPARATELY from the chat
provider (``EMBEDDING_PROVIDER``), and Anthropic users have two working choices:
an OpenAI key used for embeddings only, or Voyage AI. Voyage is a plain REST
call, deliberately not another SDK dependency.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

import httpx

# ── Provider selection ───────────────────────────────────────────────────────
# An explicit override for anything the name-based inference below cannot know:
# a proxy, a local server, an OpenAI-compatible gateway, a custom fine-tune name.
# Empty (the default) means "infer from the model name", which is what makes
# per-tier provider choice work without a second env var per tier.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").strip().lower()

# Which service answers embedding calls. Separate from the chat provider on
# purpose -- see the module docstring. "openai" covers any OpenAI-compatible
# embeddings endpoint via OPENAI_BASE_URL.
EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "openai").strip().lower()

# Base URLs. Both empty by default, i.e. each SDK's own default. OPENAI_BASE_URL
# is what points the "openai" provider at OpenRouter, Groq, Together, LM Studio,
# Ollama or any other OpenAI-compatible server.
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip()
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "").strip()

# Anthropic requires max_tokens on every call. This is the fallback for callers
# that pass none -- generous, because the final judge legitimately emits
# thousands of tokens per call and a silent truncation there parses as a failure
# and vanishes down a fail-open path.
ANTHROPIC_MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS", "8192"))

VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")
VOYAGE_EMBED_MODEL = os.getenv("VOYAGE_EMBED_MODEL", "voyage-3.5-lite")

_ANTHROPIC_NAME_HINTS = ("claude", "anthropic")
_OPENAI_NAME_HINTS = ("gpt", "o1", "o3", "o4", "text-embedding", "davinci", "babbage")


def provider_for(model: str) -> str:
    """Which provider serves `model`: "anthropic" or "openai".

    Name-based, so setting one env var per tier is enough to move that tier to a
    different vendor. LLM_PROVIDER overrides it for names neither family claims
    -- which is also the escape hatch for an OpenAI-compatible proxy serving
    Claude models under their real names (set LLM_PROVIDER=openai and point
    OPENAI_BASE_URL at it).
    """
    if LLM_PROVIDER:
        return LLM_PROVIDER
    name = (model or "").lower()
    if any(h in name for h in _ANTHROPIC_NAME_HINTS):
        return "anthropic"
    if any(name.startswith(h) or h in name for h in _OPENAI_NAME_HINTS):
        return "openai"
    # Unknown name and no override: OpenAI is the historical default, and the
    # OpenAI wire format is what every compatible third-party server speaks.
    return "openai"


# ── Result shapes ────────────────────────────────────────────────────────────
@dataclass
class _PromptTokensDetails:
    """Mirrors OpenAI's `usage.prompt_tokens_details` so the usage object below
    is duck-type-compatible with what `_record_llm_usage` already reads."""
    cached_tokens: int = 0


@dataclass
class Usage:
    """OpenAI-shaped usage, whichever provider produced it.

    Deliberately re-shaped rather than passed through: `full_auto._record_llm_usage`
    and the Settings token panel read these exact attribute names, and the whole
    point of the per-stage cache-hit-rate number is that it stays comparable
    across a provider change.
    """
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    prompt_tokens_details: _PromptTokensDetails = field(default_factory=_PromptTokensDetails)


@dataclass
class ChatResult:
    text: str = ""
    usage: Usage | None = None
    finish_reason: str = ""
    model: str = ""
    provider: str = ""


# ── Clients (cached per (provider, timeout) so SDK connection pools are reused) ─
_CLIENTS: dict[tuple, object] = {}
_CLIENT_LOCK = threading.Lock()


class MissingProviderSDK(RuntimeError):
    """Raised when a model routed to a provider whose SDK isn't installed.

    Carries the pip command, because the alternative -- a bare ImportError deep
    inside a pipeline stage -- reads as a crash rather than as a one-line fix.
    """


class MissingProviderKey(RuntimeError):
    """Raised when a model routed to a provider with no API key configured."""


def _openai_client(timeout: float, max_retries: int):
    key = ("openai", timeout, max_retries)
    with _CLIENT_LOCK:
        if key in _CLIENTS:
            return _CLIENTS[key]
    try:
        from openai import OpenAI
    except ImportError as e:  # pragma: no cover - dependency is in requirements
        raise MissingProviderSDK("The OpenAI SDK is not installed. Run: pip install openai") from e

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key and not OPENAI_BASE_URL:
        # A local/compatible server often needs no key, hence the base-URL escape.
        raise MissingProviderKey(
            "OPENAI_API_KEY is not set. Add it to .env, or point OPENAI_BASE_URL "
            "at a compatible server that needs no key."
        )
    kwargs = {
        "api_key": api_key or "not-needed",
        "timeout": httpx.Timeout(timeout, connect=5.0),
        "max_retries": max_retries,
    }
    if OPENAI_BASE_URL:
        kwargs["base_url"] = OPENAI_BASE_URL
    client = OpenAI(**kwargs)
    with _CLIENT_LOCK:
        _CLIENTS[key] = client
    return client


def _anthropic_client(timeout: float, max_retries: int):
    key = ("anthropic", timeout, max_retries)
    with _CLIENT_LOCK:
        if key in _CLIENTS:
            return _CLIENTS[key]
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise MissingProviderSDK(
            "A Claude model is configured but the Anthropic SDK is not installed. "
            "Run: pip install anthropic"
        ) from e

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not ANTHROPIC_BASE_URL:
        raise MissingProviderKey(
            "ANTHROPIC_API_KEY is not set, but a Claude model is configured. "
            "Add the key to .env, or switch the model env vars back to OpenAI."
        )
    kwargs = {
        "api_key": api_key or "not-needed",
        "timeout": httpx.Timeout(timeout, connect=5.0),
        "max_retries": max_retries,
    }
    if ANTHROPIC_BASE_URL:
        kwargs["base_url"] = ANTHROPIC_BASE_URL
    client = Anthropic(**kwargs)
    with _CLIENT_LOCK:
        _CLIENTS[key] = client
    return client


# ── Chat ─────────────────────────────────────────────────────────────────────
def chat(
    prompt: str,
    system: str = "",
    model: str = "",
    *,
    require_json: bool = False,
    temperature: float = 0.2,
    max_output_tokens: int = 0,
    cache_key: str = "",
    cache_retention: str = "",
    timeout: float = 90.0,
    max_retries: int = 1,
) -> ChatResult:
    """One chat completion, on whichever provider `model` belongs to.

    `require_json` asks for a bare JSON object. On OpenAI that is
    `response_format`; on Anthropic it is an assistant prefill (see the module
    docstring). Either way the caller gets text that starts with `{`.

    `cache_key` / `cache_retention` are prompt-caching hints and are always
    safe to pass: on OpenAI they are routing hints that can only cost a miss,
    and on Anthropic they set an explicit `cache_control` marker on the system
    block. Neither can ever serve the wrong content.
    """
    prov = provider_for(model)
    if prov == "anthropic":
        return _chat_anthropic(
            prompt, system, model,
            require_json=require_json, temperature=temperature,
            max_output_tokens=max_output_tokens, cache=bool(cache_key or cache_retention),
            timeout=timeout, max_retries=max_retries,
        )
    return _chat_openai(
        prompt, system, model,
        require_json=require_json, temperature=temperature,
        max_output_tokens=max_output_tokens, cache_key=cache_key,
        cache_retention=cache_retention, timeout=timeout, max_retries=max_retries,
    )


def _chat_openai(prompt, system, model, *, require_json, temperature,
                 max_output_tokens, cache_key, cache_retention,
                 timeout, max_retries) -> ChatResult:
    client = _openai_client(timeout, max_retries)
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})

    args = {"model": model, "messages": msgs, "temperature": temperature}
    if require_json:
        args["response_format"] = {"type": "json_object"}
    if cache_key:
        args["prompt_cache_key"] = cache_key
    if cache_retention:
        args["prompt_cache_retention"] = cache_retention
    if max_output_tokens:
        args["max_completion_tokens"] = max_output_tokens

    resp = client.chat.completions.create(**args)
    choice = resp.choices[0]
    raw = getattr(resp, "usage", None)
    usage = None
    if raw is not None:
        details = getattr(raw, "prompt_tokens_details", None)
        usage = Usage(
            prompt_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
            total_tokens=int(getattr(raw, "total_tokens", 0) or 0),
            prompt_tokens_details=_PromptTokensDetails(
                cached_tokens=int(getattr(details, "cached_tokens", 0) or 0) if details else 0
            ),
        )
    return ChatResult(
        text=(choice.message.content or "").strip(),
        usage=usage,
        finish_reason=getattr(choice, "finish_reason", "") or "",
        model=model,
        provider="openai",
    )


# Anthropic's temperature range is 0..1, OpenAI's is 0..2. Every call site in
# this app asks for 0, 0.2 or 1, so this clamp never actually bites today -- it
# is here so that a future caller raising the temperature gets a slightly
# different answer rather than a 400 from one provider and not the other.
def _clamp_temperature(t: float) -> float:
    return max(0.0, min(1.0, float(t)))


def _chat_anthropic(prompt, system, model, *, require_json, temperature,
                    max_output_tokens, cache, timeout, max_retries) -> ChatResult:
    client = _anthropic_client(timeout, max_retries)

    msgs: list[dict] = [{"role": "user", "content": prompt}]
    if require_json:
        # Prefilling the assistant turn with "{" is Anthropic's documented way
        # to force a bare JSON object: the model can only continue the object it
        # has apparently already started, so there is no prose preamble and no
        # code fence to strip. The brace is glued back on below, because the
        # response contains only the CONTINUATION.
        msgs.append({"role": "assistant", "content": "{"})

    args: dict = {
        "model": model,
        "messages": msgs,
        "max_tokens": max_output_tokens or ANTHROPIC_MAX_TOKENS,
        "temperature": _clamp_temperature(temperature),
    }
    if system:
        if cache:
            # Explicit cache marker. Anthropic caches the prefix UP TO AND
            # INCLUDING the marked block, which is exactly the shape every
            # prompt here has: a long fixed system prefix, then a short variable
            # payload in the user turn. The judge's ~12k-token system prompt is
            # the one that makes this worth doing.
            args["system"] = [{
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            args["system"] = system

    resp = client.messages.create(**args)

    text = "".join(
        getattr(block, "text", "") for block in (getattr(resp, "content", None) or [])
    ).strip()
    if require_json and not text.startswith("{"):
        text = "{" + text

    raw = getattr(resp, "usage", None)
    usage = None
    if raw is not None:
        cached = int(getattr(raw, "cache_read_input_tokens", 0) or 0)
        created = int(getattr(raw, "cache_creation_input_tokens", 0) or 0)
        # Anthropic reports input_tokens EXCLUSIVE of cached/created tokens,
        # where OpenAI's prompt_tokens is inclusive and cached_tokens is a
        # subset of it. Summing here is what keeps the panel's "N prompt (M
        # cached, X%)" line meaning the same thing on both providers.
        prompt_tokens = int(getattr(raw, "input_tokens", 0) or 0) + cached + created
        completion = int(getattr(raw, "output_tokens", 0) or 0)
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion,
            total_tokens=prompt_tokens + completion,
            prompt_tokens_details=_PromptTokensDetails(cached_tokens=cached),
        )

    stop = getattr(resp, "stop_reason", "") or ""
    return ChatResult(
        text=text,
        usage=usage,
        # "length" is OpenAI's word for it, and it is what `length_capped`
        # counts. Keeping the mapping here means the truncation diagnostic works
        # unchanged on Anthropic.
        finish_reason="length" if stop == "max_tokens" else stop,
        model=model,
        provider="anthropic",
    )


# ── Embeddings ───────────────────────────────────────────────────────────────
def embed(texts: list[str], model: str, *, timeout: float = 90.0) -> list[list[float]]:
    """Embed `texts`, in order.

    Anthropic has no embeddings API and the embedding pre-filter is load-bearing
    here, so this routes on EMBEDDING_PROVIDER rather than on the chat provider.
    An Anthropic user sets EMBEDDING_PROVIDER=voyage, or keeps an OpenAI key for
    this one call.
    """
    if EMBEDDING_PROVIDER == "voyage":
        return _embed_voyage(texts, timeout=timeout)
    client = _openai_client(timeout, 1)
    resp = client.embeddings.create(model=model, input=texts)
    return [e.embedding for e in resp.data]


def _embed_voyage(texts: list[str], *, timeout: float) -> list[list[float]]:
    """Voyage AI embeddings, over plain HTTP.

    Deliberately not another SDK: this is one POST, httpx is already a pinned
    dependency, and the auth-adjacent parts of this app are kept free of
    dependencies added for a single call.
    """
    if not VOYAGE_API_KEY:
        raise MissingProviderKey(
            "EMBEDDING_PROVIDER=voyage but VOYAGE_API_KEY is not set."
        )
    r = httpx.post(
        "https://api.voyageai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {VOYAGE_API_KEY}"},
        json={"model": VOYAGE_EMBED_MODEL, "input": texts, "input_type": "document"},
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    # Voyage returns an `index` per row. Sort by it rather than trusting request
    # order: every caller here relies on positional alignment with its input
    # list, and a silent re-ordering would mis-assign vectors to jobs -- which
    # would look like a ranking-quality problem, not a bug.
    return [row["embedding"] for row in sorted(data, key=lambda d: d.get("index", 0))]


# ── Startup reporting ────────────────────────────────────────────────────────
def describe_configuration(models: dict[str, str]) -> str:
    """One human-readable line per tier, for the server console at boot.

    Exists because the failure this prevents is silent: a model name the key
    cannot reach fails inside a pipeline stage that fails open, so the run
    completes, returns nothing useful, and logs a parse error rather than an
    auth error. Printing the routing at startup makes a misconfiguration visible
    before a search is ever spent on it.
    """
    lines = []
    for tier, model in models.items():
        prov = provider_for(model)
        key = "OPENAI_API_KEY" if prov == "openai" else "ANTHROPIC_API_KEY"
        have = "ok" if os.environ.get(key) else "MISSING"
        lines.append(f"  {tier:<10} {model:<28} via {prov:<9} ({key}: {have})")
    return "\n".join(lines)
