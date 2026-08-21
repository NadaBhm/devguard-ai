"""Lightweight remote-repo inspection, without cloning.

Sibling to ``src/lib/repo.py``'s ``clone_repo`` (same fail-loudly convention:
raises ``RuntimeError``, never returns a sentinel), but for callers that only
need to know "what's the latest commit on this branch" -- the update-check
flow shouldn't pay for a full clone just to answer that question.
"""

from __future__ import annotations

import subprocess

_DEFAULT_TIMEOUT_SECONDS: int = 15


def latest_remote_sha(repo_url: str, branch: str, *, timeout: int = _DEFAULT_TIMEOUT_SECONDS) -> str:
    """Return the full 40-character commit SHA at the tip of ``branch``.

    Uses ``git ls-remote`` -- no clone, no GitHub/GitLab API token needed,
    works against any host `git` itself can reach.

    Raises:
        RuntimeError: git is unavailable, the remote/branch doesn't resolve,
            or the command times out.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", repo_url, f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git ls-remote timed out after {timeout} seconds") from exc
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed or not in PATH") from exc

    if result.returncode != 0:
        raise RuntimeError(f"git ls-remote failed: {result.stderr.strip()}")

    line = result.stdout.strip()
    if not line:
        raise RuntimeError(f"Branch {branch!r} not found on {repo_url!r}")

    sha = line.split()[0]
    if len(sha) != 40:
        raise RuntimeError(f"Unexpected git ls-remote output: {line!r}")

    return sha
