"""Pytest bootstrap: make the agentInfraCost package root importable.

Modules in this agent import their siblings with flat top-level names
(``from models.input_schema import ...``, ``from core.decision_engine import
...``) rather than a fully-qualified dotted path, matching the convention
already used by ``src/subgroup2/orchestrator``. That requires the
``agentInfraCost`` directory itself (not its parent) to be on ``sys.path``.
"""

import sys
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))
