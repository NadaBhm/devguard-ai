"""Server-side validation for manually edited artifacts.
Edits apply to orchestrator state and deploy later, so syntax-valid saves are
enforced here rather than failing deep inside Terraform at apply time. ``.tf``:
brace/quote balance plus ``terraform fmt`` parse when CLI available; Dockerfile
needs ``FROM``; docker-image.json must be JSON with a "name". Validators return
an error string, or None when valid.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_ALLOWED_FILE_PATHS = frozenset({
    "main.tf",
    "variables.tf",
    "outputs.tf",
    "Dockerfile",
    "docker-image.json",
})


def allowed_file_path(file_path: str) -> bool:
    if file_path in _ALLOWED_FILE_PATHS:
        return True
    # Multi-container: Dockerfiles live under their build context, not just repo root.
    if file_path.endswith("/Dockerfile"):
        parent = file_path[:-len("/Dockerfile")]
        return bool(parent) and not parent.startswith("/") and ".." not in parent.split("/")
    return False


def _balanced_structure(content: str) -> bool:
    return content.count("{") == content.count("}") and content.count('"') % 2 == 0


def _terraform_fmt_check(content: str) -> str | None:
    """Syntax-check via ``terraform fmt`` on a temp file (no providers/init needed)."""
    tf = shutil.which("terraform")
    if not tf:
        return None  # CLI unavailable — structural check already passed
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "main.tf"
            path.write_text(content, encoding="utf-8")
            proc = subprocess.run(
                [tf, "fmt", "-check", "-write=false", str(path)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # 0 = parses; 1 = would reformat (valid, formatting preserved on purpose); else error.
            if proc.returncode not in (0, 1):
                return (proc.stderr or proc.stdout or "terraform fmt failed").strip()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("terraform fmt validation unavailable: %s", exc)
        return None
    return None


def validate_terraform(content: str) -> str | None:
    if not content.strip():
        return "Terraform file is empty."
    if not _balanced_structure(content):
        return "Unbalanced braces or quotes — the file will not parse."
    return _terraform_fmt_check(content)


def validate_dockerfile(content: str) -> str | None:
    if not content.strip():
        return "Dockerfile is empty."
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FROM "):
            return None
    return 'Dockerfile must contain a "FROM <image>" instruction.'


def validate_image_json(content: str) -> str | None:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return "docker-image.json is not valid JSON."
    if not isinstance(data, dict):
        return "docker-image.json must be a JSON object."
    if "name" not in data:
        return 'docker-image.json must contain a "name".'
    return None


_VALIDATORS = {
    "main.tf": validate_terraform,
    "variables.tf": validate_terraform,
    "outputs.tf": validate_terraform,
    "Dockerfile": validate_dockerfile,
    "docker-image.json": validate_image_json,
}


def validate_artifact(file_path: str, content: str) -> str | None:
    validator = _VALIDATORS.get(file_path)
    if validator is None:
        return f"Unsupported artifact: {file_path}"
    return validator(content)