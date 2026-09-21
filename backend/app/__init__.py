"""Package bootstrap: make the repo root importable.

Two modules at the repo root are imported by name from inside this package --
`full_auto` (the search engine) and `llm_providers` (the OpenAI/Anthropic shim).
`config.py` has always inserted the root on `sys.path` for the first of those,
but that only works when `config` happens to be imported first, which is true of
every path through `main.py` and not true of, say, importing a single service
module in a REPL or a script.

Doing it here instead makes it true for every import of this package, in every
order, at a cost of one path check. Kept minimal on purpose -- no side effects
beyond the path, and no imports of our own modules, so importing this package
stays cheap.
"""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
