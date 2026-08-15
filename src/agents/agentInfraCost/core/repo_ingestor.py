"""Whole-repo ingestion for the InfraCost LLMs.

At Human Gate 2 the user may ask to regenerate the infrastructure from a
free-form prompt ("make it cheaper", "use two AZs", ...). Without repo
context the OpenRouter LLMs -- the architecture advisor, the deployment
advisor and especially the Terraform refiner -- only ever see the previously
generated Terraform files, a Dockerfile and the prompt. This module gives
them the *entire* repository, via a map-reduce pass:

  1. Every text/config file under the clone is read (broad extension set,
     ignore-dirs, binary and size guards -- never the whole tree blindly).
  2. The files are chunked (a file is never split unless it alone exceeds the
     chunk budget) and each chunk is given to the LLM with a strict JSON
     contract: return ONLY the infrastructure-relevant facts it contains
     (entrypoints, ports, health checks, databases, build tooling, container
     config, env vars, concurrency signals).
  3. The per-chunk fact lists are merged into one digest string that the
     pipeline threads through ``RepoAnalysisInput.repo_context`` into every
     LLM prompt.

Same fail-soft contract as every other LLM call in this agent: any failure
(no ``OPENROUTER_API_KEY``, network error, malformed reply, an unreadable or
empty repo) collapses to ``None`` -- the pipeline proceeds exactly as before,
without repo context. The digest is repo-only, so it is cached per
``commit_sha``: repeated regeneration rounds on the same analyzed commit do
not pay for the map pass again.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

from core.constants import (
    REPO_CHUNK_BYTES,
    REPO_DIGEST_TIMEOUT_SECONDS,
    REPO_MAX_BYTES,
    REPO_MAX_CHUNKS,
    REPO_MAX_FILE_BYTES,
)
from core.llm_provider import call_llm

logger = logging.getLogger(__name__)

#: Directories that are never under any circumstances read (dependency/vendor
#: trees, VCS internals, build output).
_IGNORE_DIRS: Final[frozenset[str]] = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", "dist", "build", "target", "vendor",
    "bower_components", ".idea", ".vscode", ".tox", "site-packages",
    ".terraform", "coverage", ".next",
})

#: File suffixes that count as readable source/config text.
_TEXT_SUFFIXES: Final[frozenset[str]] = frozenset({
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".go", ".java",
    ".rb", ".rs", ".php", ".sh", ".bash", ".zsh", ".sql", ".tf", ".tfvars",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".md",
    ".txt", ".html", ".css", ".scss", ".sass", ".vue", ".svelte", ".xml",
    ".proto", ".graphql", ".gql", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs",
    ".swift", ".kt", ".kts", ".dart", ".r", ".jl", ".lua", ".pl", ".ex",
    ".exs", ".clj", ".scala", ".groovy", ".gradle", ".properties",
})

#: Suffix/name patterns that are never read (lockfiles, minified bundles).
_IGNORE_SUFFIXES: Final[frozenset[str]] = frozenset({
    ".lock", ".min.js", ".min.css", ".map", ".svg", ".png", ".jpg", ".jpeg",
    ".gif", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip", ".tar",
    ".gz", ".whl", ".so", ".dll", ".dylib", ".exe", ".class", ".pyc",
})

#: Extensionless/config entrypoint files matched by name (``fnmatch``-style).
#: Lockfiles are absent on purpose: every ``*.lock`` / ``-lock.json`` variant
#: is a vendor dependency manifest, and ``_boring_name`` skips them anyway.
_NAME_PATTERNS: Final[tuple[str, ...]] = (
    "Dockerfile*", "docker-compose*.yml", "docker-compose*.yaml", "Makefile",
    "makefile", "Procfile", "go.mod", "go.sum", "requirements*.txt",
    "Pipfile", "pyproject.toml", "package.json", "Gemfile", "Cargo.toml",
    "pom.xml", "build.gradle", "build.gradle.kts", ".env", ".env.*",
    ".dockerignore", ".gitignore", ".gitattributes", ".npmrc", ".babelrc",
    ".eslintrc*", "tsconfig*.json", "jest.config.*", "vite.config.*",
    "webpack.config.*", "alembic.ini", "setup.py", "setup.cfg",
)

#: Per-chunk facts from the digest pass.
_DIGEST_HEADER: Final[str] = "Faits extraits du dépôt (pertinents pour l'infrastructure) :"

_SYSTEM_INSTRUCTION: Final[str] = (
    "Tu es un ingénieur d'infrastructure AWS. On te donne un fragment de code "
    "d'un dépôt. Extrais UNIQUEMENT les faits pertinents pour dimensionner ou "
    "déployer l'infrastructure : langages et frameworks, points d'entrée et "
    "handlers, ports HTTP, chemins de health check, bases de données, files ou "
    "tâches planifiées, configuration conteneur/Docker, build tooling, "
    "dépendances clés, variables d'environnement attendues, tout signal de "
    "charge ou de concurrence. Ignore tout le reste - pas de commentaires, pas "
    "de résumé, uniquement des faits. Réponds uniquement avec un JSON de la "
    'forme {"facts": ["fact 1", "fact 2"]}, sans texte autour. Si le fragment '
    'ne contient rien d\'utile, renvoie {"facts": []}.'
)


def clear_digest_cache() -> None:
    """Wipe the in-process digest cache (used by tests)."""
    _digest_cache.clear()


_digest_cache: dict[str, str] = {}


# =============================================================================
# Reading the repository
# =============================================================================


def _ignore_part(parts: tuple[str, ...]) -> bool:
    return any(part in _IGNORE_DIRS for part in parts)


def _boring_name(name: str, suffix: str) -> bool:
    if suffix in _IGNORE_SUFFIXES:
        return True
    lowered = name.lower()
    if lowered.endswith("-lock.json") or lowered.endswith(".lock"):
        return True
    if any(pat in lowered for pat in ("min.js", "bundle.js", ".map")):
        return True
    return False


def _is_binary(content: bytes) -> bool:
    return b"\x00" in content[:8192]


def read_all_text_files(repo_path: Path) -> list[dict[str, str]]:
    """Read every readable text/config file in ``repo_path``.

    Respects ``_IGNORE_DIRS``, per-file and total byte budgets, and skips
    binary/lock/minified content. The total read is capped at
    ``REPO_MAX_BYTES`` so an enormous repository cannot balloon the digest
    pass. Returns ``[{"path", "content"}, ...]`` sorted by path for
    determinism (never an error, never a partial read poisoning the digest).
    """
    files: list[dict[str, str]] = []
    total_bytes = 0

    candidate_paths = sorted(p for p in repo_path.rglob("*") if p.is_file())
    for file_path in candidate_paths:
        if total_bytes >= REPO_MAX_BYTES:
            break
        if _ignore_part(file_path.relative_to(repo_path).parts):
            continue

        suffix = file_path.suffix.lower()
        name = file_path.name
        if not (
            suffix in _TEXT_SUFFIXES
            or any(file_path.match(pat) for pat in _NAME_PATTERNS)
        ):
            continue
        if _boring_name(name, suffix):
            continue
        if file_path.stat().st_size > REPO_MAX_FILE_BYTES:
            continue

        try:
            content = file_path.read_bytes()
        except OSError:
            continue
        if _is_binary(content):
            continue

        text = content.decode("utf-8", errors="replace")
        if not text.strip():
            continue

        files.append({
            "path": file_path.relative_to(repo_path).as_posix(),
            "content": text,
        })
        total_bytes += len(text)

    return files


# =============================================================================
# Chunking
# =============================================================================


def _file_block(path: str, content: str, *, part: str | None = None) -> str:
    label = f"{path} (partie {part})" if part else path
    return f"=== {label} ===\n{content}"


def chunk_files(files: list[dict[str, str]], chunk_bytes: int = REPO_CHUNK_BYTES) -> list[str]:
    """Group whole files into chunks, splitting only oversized files.

    A chunk is a ``=== path ===``-prefixed concatenation of files. A file
    that alone exceeds ``chunk_bytes`` is split into repeated same-labeled
    blocks rather than merged with neighbours, so reads stay deterministic
    and a single giant file cannot blow past the token budget.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    def _flush() -> None:
        nonlocal current, current_size
        if current:
            chunks.append("\n".join(current))
            current = []
            current_size = 0

    for entry in files:
        path = entry["path"]
        content = entry["content"]
        if len(content) > chunk_bytes:
            _flush()
            for i in range(0, len(content), chunk_bytes):
                chunks.append(
                    _file_block(path, content[i : i + chunk_bytes], part=f"{i // chunk_bytes + 1}")
                )
            continue

        block = _file_block(path, content)
        if current and current_size + len(block) > chunk_bytes:
            _flush()
        current.append(block)
        current_size += len(block)

    _flush()
    return chunks


# =============================================================================
# Map-reduce digest
# =============================================================================


def _extract_facts(chunk: str) -> list[str]:
    """One map step: ask the LLM for the infra facts inside ``chunk``.

    Any failure (no key, network, malformed reply) returns ``[]`` — the
    digest degrades gracefully chunk by chunk.
    """
    raw_text = call_llm(
        prompt=(
            "=== FRAGMENT DU DÉPÔT ===\n"
            f"{chunk}\n\n"
            "Extrais UNIQUEMENT les faits pertinents pour l'infrastructure en JSON."
        ),
        system_instruction=_SYSTEM_INSTRUCTION,
        timeout=REPO_DIGEST_TIMEOUT_SECONDS,
    )
    if not raw_text:
        return []

    try:
        payload = json.loads(raw_text)
        facts = payload.get("facts")
        if not isinstance(facts, list):
            return []
        return [str(fact).strip() for fact in facts if str(fact).strip()]
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        logger.warning("Repo-digest chunk reply failed to parse: %s", exc)
        return []


def ingest_repo(
    repo_path: str | Path,
    job_id: str,
    *,
    commit_sha: str | None = None,
) -> str | None:
    """Map-reduce the whole repository into an infra-facts digest.

    Args:
        repo_path: the (re-)cloned repository directory.
        job_id: orchestrator job id, for logging.
        commit_sha: analyzed commit — the digest is repo-only, so it is
            cached per commit and regeneration rounds on the same commit
            never re-pay for the map pass.

    Returns:
        The merged digest string, or ``None`` on any failure (fail-soft —
        the pipeline then runs exactly as before, without repo context).
    """
    if commit_sha and commit_sha not in ("", "unknown") and commit_sha in _digest_cache:
        logger.info("[%s] Reusing cached repo digest for commit %s", job_id, commit_sha)
        return _digest_cache[commit_sha]

    try:
        files = read_all_text_files(Path(repo_path))
    except Exception as exc:
        logger.warning("[%s] Could not read repo for digest: %s", job_id, exc)
        return None

    if not files:
        logger.info("[%s] No readable files to digest; regenerating without repo context", job_id)
        return None

    chunks = chunk_files(files)[:REPO_MAX_CHUNKS]
    all_facts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        logger.info(
            "[%s] Digesting repo chunk %d/%d (%d files)",
            job_id, index, len(chunks), len(files),
        )
        all_facts.extend(_extract_facts(chunk))

    if not all_facts:
        logger.info("[%s] Repo digest produced no facts; keeping original behavior", job_id)
        return None

    digest = (
        f"{_DIGEST_HEADER}\n" + "\n".join(f"- {fact}" for fact in all_facts)
    )
    if commit_sha and commit_sha not in ("", "unknown"):
        _digest_cache[commit_sha] = digest
    logger.info("[%s] Repo digest ready (%d facts)", job_id, len(all_facts))
    return digest
