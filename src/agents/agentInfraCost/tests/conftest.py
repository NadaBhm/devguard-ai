"""Make the agentInfraCost package root importable.

Siblings import with flat top-level names (``from core.decision_engine
import ...``), so ``agentInfraCost`` itself — not its parent — must be on
``sys.path``.
"""

import sys
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

# main.py calls load_dotenv() at import, so a developer's local .env would
# leak real credentials into every later test in the same process. Clear
# them before every test so the suite is identical on any machine.
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
    # Real scoring must run; the ECS force (default on for the ECS-only
    # pipeline) is disabled here.
    monkeypatch.setenv("DEVGUARD_FORCE_COMPUTE_ECS", "0")
