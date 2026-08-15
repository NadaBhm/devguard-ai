"""Pytest bootstrap: make the agentInfraCost package root importable.

Modules in this agent import their siblings with flat top-level names
(``from models.input_schema import ...``, ``from core.decision_engine import
...``) rather than a fully-qualified dotted path, matching the convention
already used by ``src/subgroup2/orchestrator``. That requires the
``agentInfraCost`` directory itself (not its parent) to be on ``sys.path``.
"""

import sys
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

# main.py calls load_dotenv() at import time (see main.py) so a real local
# .env file's credentials persist across terminal sessions. But that means
# once anything imports main.py (test_main.py does, via TestClient), a real
# .env on the developer's machine leaks its values into every test that
# runs afterward in the same pytest process -- tests must never depend on
# what happens to exist on a given machine. Clear these before every test,
# regardless of which file, so the suite is identical on any machine
# whether or not a .env is present.
_OPTIONAL_CREDENTIAL_ENV_VARS = (
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENROUTER_MODEL",
    "OPENROUTER_PROVIDER",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
)


@pytest.fixture(autouse=True)
def _no_real_credentials_leak_into_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _OPTIONAL_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # Unit tests exercise the real scoring logic; the ECS force (default on
    # for the real pipeline, since DeployOps is ECS-only) is disabled here.
    monkeypatch.setenv("DEVGUARD_FORCE_COMPUTE_ECS", "0")
