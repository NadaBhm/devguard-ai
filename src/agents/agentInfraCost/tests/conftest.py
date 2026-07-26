"""Makes the `agentInfraCost` package importable when pytest is run from
anywhere, without requiring the whole `src/` tree to be turned into a
Python package (src/frontend, src/lib etc. are unrelated to this agent).

Also puts the repo root on sys.path so `from src.shared.llm.gemini import
...` (used by llm_enrichment.py) resolves, matching the `src.<pkg>...`
import convention hinted at elsewhere in the repo (see
src/lib/terraform/runner.py's module docstring)."""

import sys
from pathlib import Path

_AGENTS_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[4]

for _path in (_AGENTS_DIR, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
