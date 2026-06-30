"""Thin OpenAI wrapper shared by parsing + suggestion services.

Kept independent of the search engine (full_auto.py) so the app's lightweight LLM
calls don't drag in crawl4ai/playwright at import time."""
import json
import os
from functools import lru_cache

from openai import OpenAI

# Match the model family the engine uses for cheap calls.
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "gpt-5.4-nano-2026-03-17")


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    return OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


def _clean_json(raw: str) -> str:
    return (
        raw.strip()
        .removeprefix("```json")
        .removeprefix("```")
        .removesuffix("```")
        .strip()
    )


def llm_json(prompt: str, system: str = "", model: str = CHEAP_MODEL) -> dict:
    """Call the model and parse a JSON object out of the reply. Returns {} on failure."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    try:
        resp = _client().chat.completions.create(
            model=model,
            messages=msgs,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        return json.loads(_clean_json(resp.choices[0].message.content))
    except Exception:
        return {}
