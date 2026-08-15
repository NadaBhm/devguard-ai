"""Repository clone helper shared across agents.

``src/agents/codesec/agent.py`` grew its own private clone routine while the
InfraCost adapter (``src/agents/orchestrator/agent_adapters.py``) needs the
same shallow-clone behaviour at Human Gate 2: re-clone the analyzed repo so
the OpenRouter refiner can digest the whole codebase during regeneration
(CodeSec deletes its clone the moment analysis finishes). Extracted here so
both flows share one implementation instead of drifting.

Fails loudly on every failure (raises ``RuntimeError``); callers decide
whether that is fatal (CodeSec aborts the run) or fail-soft (Gate 2
regenerates without repo context).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_DEFAULT_CLONE_TIMEOUT_SECONDS: int = 60


def clone_repo(
    repo_url: str,
    target_dir: str | Path,
    *,
    max_size_mb: int = 500,
    max_files: int = 10_000,
    timeout: int = _DEFAULT_CLONE_TIMEOUT_SECONDS,
) -> Path:
    """Shallow-clone a public repository into ``target_dir``.

    Args:
        repo_url: HTTPS URL of the repository.
        target_dir: destination directory. Recreated if it already exists —
            a stale clone must never be reused.
        max_size_mb: abort if the cloned tree exceeds this many MB.
        max_files: abort if the cloned tree contains more files than this.
        timeout: per-``git clone`` attempt timeout in seconds.

    Returns:
        The ``target_dir`` path, once validated.

    Raises:
        RuntimeError: git is unavailable, the clone fails or times out, or
            the cloned tree exceeds the size/file limits.
    """
    target = Path(target_dir)
    if target.exists():
        shutil.rmtree(target)

    try:
        result = subprocess.run(
            [
                "git", "clone", "--depth=1", "--single-branch",
                "--branch=main", repo_url, str(target),
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0:
            # 'main' is the modern default but 'master' still exists — try
            # it before giving up.
            shutil.rmtree(target, ignore_errors=True)
            result = subprocess.run(
                [
                    "git", "clone", "--depth=1", "--single-branch",
                    "--branch=master", repo_url, str(target),
                ],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0:
                shutil.rmtree(target, ignore_errors=True)
                raise RuntimeError(f"Git clone failed: {result.stderr}")
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(f"Git clone timed out after {timeout} seconds") from exc
    except FileNotFoundError as exc:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError("Git is not installed or not in PATH") from exc

    total_size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    total_size_mb = total_size / (1024 * 1024)
    if total_size_mb > max_size_mb:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"Repository exceeds {max_size_mb} MB limit ({total_size_mb:.1f} MB)"
        )

    total_files = sum(1 for _ in target.rglob("*") if _.is_file())
    if total_files > max_files:
        shutil.rmtree(target, ignore_errors=True)
        raise RuntimeError(
            f"Repository exceeds {max_files} file limit ({total_files} files)"
        )

    return target
