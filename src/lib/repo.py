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

_DEFAULT_CLONE_TIMEOUT_SECONDS: int = 300


def _git(args: list[str], timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run a git command, raising RuntimeError on failure/timeout."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Git {' '.join(args[:2])} timed out after {timeout} seconds") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed or not in PATH") from exc
    if result.returncode != 0:
        raise RuntimeError(f"Git {' '.join(args[:3])} failed: {result.stderr.strip()}")
    return result


def _clone_pinned(repo_url: str, target: Path, commit_sha: str, timeout: int) -> None:
    """Clone an exact commit. GitHub serves arbitrary reachable SHAs via
    ``git fetch <sha>`` (uploadpack.allowAnySHA1InWant), so a depth-1 fetch of
    the SHA itself is the cheapest correct path; a full clone + checkout is
    the fallback for hosts that refuse it (e.g. local fixtures without the
    config)."""
    target.mkdir(parents=True, exist_ok=False)
    try:
        _git(["init"], timeout, cwd=target)
        _git(["remote", "add", "origin", repo_url], timeout, cwd=target)
        _git(["fetch", "--depth=1", "origin", commit_sha], timeout, cwd=target)
        _git(["checkout", "FETCH_HEAD"], timeout, cwd=target)
    except RuntimeError:
        shutil.rmtree(target, ignore_errors=True)
        # Fallback: full clone then checkout the SHA.
        try:
            _git(["clone", "--no-checkout", repo_url, str(target)], timeout)
            _git(["checkout", commit_sha], timeout, cwd=target)
        except RuntimeError as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise RuntimeError(
                f"Could not pin commit {commit_sha}: {exc}"
            ) from exc


def clone_repo(
    repo_url: str,
    target_dir: str | Path,
    *,
    max_size_mb: int = 500,
    max_files: int = 10_000,
    timeout: int = _DEFAULT_CLONE_TIMEOUT_SECONDS,
    commit_sha: str | None = None,
) -> Path:
    """Shallow-clone a public repository into ``target_dir``, aborting on
    size/file-count limits or timeout. ``target_dir`` is recreated if it
    already exists — a stale clone must never be reused.

    ``commit_sha`` pins the checkout to that exact commit when provided
    ("HEAD"/None keeps default-branch tip behaviour).

    Raises RuntimeError on limit/timeout violations.
    """
    target = Path(target_dir)
    if target.exists():
        shutil.rmtree(target)

    if commit_sha and commit_sha != "HEAD":
        try:
            _clone_pinned(repo_url, target, commit_sha, timeout)
        except RuntimeError:
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            raise
    else:
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
                    # Non-standard default branch (e.g. canary) — let git resolve it.
                    shutil.rmtree(target, ignore_errors=True)
                    result = subprocess.run(
                        ["git", "clone", "--depth=1", repo_url, str(target)],
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
