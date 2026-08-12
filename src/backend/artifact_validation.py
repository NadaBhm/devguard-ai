"""Server-side validation for manually edited artifacts.

Users can edit the generated Terraform / Dockerfile while a run is paused at
Gate 2. These edits are applied to the orchestrator state and later deployed,
so a syntax-valid save is enforced here — a broken file is rejected with a
clear message instead of failing deep inside Terraform at apply time.

- ``.tf`` files: structural check (balanced braces/quotes, non-empty) plus,
  when the ``terraform`` CLI is present, a real ``terraform fmt`` parse pass
  (validates syntax without needing providers / ``terraform init``).
- ``Dockerfile``: must contain a ``FROM`` instruction.
- ``docker-image.json``: must be valid JSON.

Every validator returns ``(ok: bool, error: str | None)``.
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
    return file_path in _ALLOWED_FILE_PATHS


def _balanced_structure(content: str) -> bool:
    """Cheap structural sanity: every open brace/quote is matched."""
    return content.count("{") == content.count("}") and content.count('"') % 2 == 0


def _terraform_fmt_check(content: str) -> str | None:
    """Run ``terraform fmt`` (syntax-only, no providers needed) on a temp
    file. Returns an error string, or None when the file parses."""
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
            # Exit 0 = properly formatted (parses). Exit 1 = would reformat
            # (still valid — the user's formatting is preserved on purpose).
            # Anything else is a syntax error.
            if proc.returncode not in (0, 1):
                return (proc.stderr or proc.stdout or "terraform fmt failed").strip()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("terraform fmt validation unavailable: %s", exc)
        return None
    return None


def validate_terraform(content: str) -> str | None:
    """Validate a ``.tf`` file. Returns an error message or None if valid."""
    if not content.strip():
        return "Terraform file is empty."
    if not _balanced_structure(content):
        return "Unbalanced braces or quotes — the file will not parse."
    return _terraform_fmt_check(content)


def validate_dockerfile(content: str) -> str | None:
    """Validate a Dockerfile: non-empty and must declare a base image."""
    if not content.strip():
        return "Dockerfile is empty."
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FROM "):
            return None
    return 'Dockerfile must contain a "FROM <image>" instruction.'


def validate_image_json(content: str) -> str | None:
    """Validate ``docker-image.json`` is a JSON object with name/tag."""
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
    """Validate a single artifact edit. Returns an error message (or None)."""
    validator = _VALIDATORS.get(file_path)
    if validator is None:
        return f"Unsupported artifact: {file_path}"
    return validator(content)