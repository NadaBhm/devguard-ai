import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")

# Tests must never silently flip real agents on via .env (config.load_dotenv
# loads it into the process env). Forcing these to "0" keeps the suite
# hermetic: no LLM/network calls, no flaky hangs from real InfraCost/CodeSec.
for _flag in (
    "DEVGUARD_REAL_AGENTS",
    "DEVGUARD_REAL_CODESEC",
    "DEVGUARD_REAL_INFRACOST",
    "DEVGUARD_REAL_DEPLOYOPS",
    "DEVGUARD_REAL_RAG",
):
    os.environ[_flag] = "0"
