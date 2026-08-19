"""
CodeSec Stack Detection Scanner
Detects primary language, frameworks, database, build tool, and container
information from repository file list and content.

US-1.1.2: As a user, I want to know my project's tech stack so that I
understand its architecture. Detection accuracy target: >=80% on test repos.

Design Decisions:
- Heuristic-based detection using filename patterns and content grepping.
- No arbitrary code execution — purely static file analysis.
- Confidence score computed from match strength and file coverage.
- Extensible indicator registry in config.py for new technologies.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..config import STACK_INDICATORS
from ..models import ContainerInfo, StackDetection
from . import ScannerError, find_dockerfiles, find_files, is_dockerfile_rel_path, read_file_safe

logger = logging.getLogger(__name__)


def _is_dockerfile_path(rel_path: str) -> bool:
    """True when a repo-relative path is an actual Dockerfile.

    Accepts ``Dockerfile``, ``Dockerfile.prod``, ``app.Dockerfile``, etc. —
    the basename must be (case-insensitively) exactly ``dockerfile``, start
    with ``dockerfile.`` (variant suffix), or end with ``.dockerfile``.
    Rejects templates and docs that merely contain the word, e.g.
    ``nginx.dockerfile.twig`` (a Twig template) or ``Dockerfile.example.txt``.
    """
    return is_dockerfile_rel_path(rel_path)


# File extensions that contribute to language LOC counting
LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".r": "r",
    ".m": "objective-c",
    ".dart": "dart",
    ".sh": "shell",
    ".dockerfile": "dockerfile",
    ".tf": "terraform",
    ".tfvars": "terraform",
}

# Service names / image tags that indicate a public web entrypoint. Used as a
# tiebreaker when a compose file maps host ports on several services (e.g. a
# gateway + separate API services all published on the host).
_ENTRYPOINT_NAME_HINTS = (
    "gateway", "proxy", "front", "web", "app", "ui", "nginx", "caddy", "traefik",
)
# Host ports most commonly bound by a public-facing web service. Used to rank
# compose services that expose host ports before picking the primary.
_ENTRYPOINT_PORT_PRIORITY = (80, 443, 8080, 3000, 8000)


def _parse_compose(repo_path: Path, compose_files: list[str]) -> dict[str, Any] | None:
    """Parse the first docker-compose file found; returns the services map."""
    try:
        import yaml  # local import: only needed for compose-aware primaries
    except ImportError:
        return None
    for path in compose_files:
        raw = read_file_safe(repo_path / path, max_size_mb=1)
        if not raw:
            continue
        try:
            data = yaml.safe_load(raw)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("services"), dict):
            return data["services"]
    return None


def _dockerfile_path_for_service(
    compose_dir: str, service_name: str, service: dict[str, Any]
) -> str | None:
    """Best-effort map of a compose service to its Dockerfile path.

    Handles both ``build: ./dir`` and ``build: {context, dockerfile}`` forms.
    Returns a repo-relative path, or None if it can't be derived.
    """
    build = service.get("build")
    if isinstance(build, str):
        context = build.strip("./") or "."
        return f"{context}/Dockerfile" if context != "." else "Dockerfile"
    if isinstance(build, dict):
        context = (build.get("context") or ".").strip("./") or "."
        df_rel = build.get("dockerfile")
        base = f"{compose_dir}/{context}".strip("/") if compose_dir else context
        if df_rel:
            return f"{base}/{df_rel.lstrip('./')}" if base != "." else df_rel.lstrip("./")
        return f"{base}/Dockerfile" if base != "." else "Dockerfile"
    # No build context: match by service dir name (common convention).
    return f"{service_name}/Dockerfile"


def _primary_dockerfile_from_compose(
    repo_path: Path, compose_files: list[str], dockerfile_files: list[str]
) -> str | None:
    """Pick the primary Dockerfile from compose host-port mappings.

    Compose services with host ``ports:`` are candidates for the public
    entrypoint. Rank them by conventional web ports, then entrypoint-name
    hints, then first-match. Returns None when there's no useful signal so the
    caller keeps scan order.
    """
    services = _parse_compose(repo_path, compose_files)
    if not services:
        return None

    compose_path = compose_files[0]
    compose_dir = str(Path(compose_path).parent).lstrip(".")
    candidates: list[tuple[tuple[int, int], str]] = []  # ((port_rank, hint_rank), dockerfile)
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        ports = service.get("ports")
        if not ports:
            continue
        host_port: int | None = None
        for entry in ports:
            if not isinstance(entry, str):
                continue
            # "3000:3000" or "127.0.0.1:3000:3000" or "3000"
            pieces = entry.split(":")
            try:
                candidate = int(pieces[-2] if len(pieces) >= 2 else pieces[0])
            except (ValueError, IndexError):
                continue
            if 1 <= candidate <= 65535:
                host_port = candidate
                break
        if host_port is None:
            continue
        df_path = _dockerfile_path_for_service(compose_dir, service_name, service)
        if not df_path or df_path not in dockerfile_files:
            continue
        try:
            port_rank = _ENTRYPOINT_PORT_PRIORITY.index(host_port)
        except ValueError:
            port_rank = len(_ENTRYPOINT_PORT_PRIORITY)
        name_lower = service_name.lower()
        try:
            hint_rank = next(
                i for i, hint in enumerate(_ENTRYPOINT_NAME_HINTS) if hint in name_lower
            )
        except StopIteration:
            hint_rank = len(_ENTRYPOINT_NAME_HINTS)
        candidates.append(((port_rank, hint_rank), df_path))

    if not candidates:
        return None
    # Stable sort: lower (port_rank, hint_rank) wins; tie keeps scan order.
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def detect_stack(repo_path: Path) -> StackDetection:
    """
    Analyze a cloned repository and detect its technology stack.
    """
    if not repo_path.exists() or not repo_path.is_dir():
        raise ScannerError(f"Repository path does not exist or is not a directory: {repo_path}")

    logger.info("Starting stack detection for: %s", repo_path)

    # Gather all files (respecting .gitignore would be a future enhancement)
    all_files: list[Path] = []
    for file_path in repo_path.rglob("*"):
        if file_path.is_file():
            all_files.append(file_path)

    rel_paths = [f.relative_to(repo_path).as_posix() for f in all_files]
    filenames = [f.name for f in all_files]

    # --- Language Detection via file extensions ---
    lang_counts: dict[str, int] = {}
    for f in all_files:
        ext = f.suffix.lower()
        if ext in LANGUAGE_EXTENSIONS:
            lang = LANGUAGE_EXTENSIONS[ext]
            # Approximate LOC by counting non-empty lines
            content = read_file_safe(f, max_size_mb=1)
            if content:
                loc = sum(1 for line in content.splitlines() if line.strip())
                lang_counts[lang] = lang_counts.get(lang, 0) + loc

    primary_language = "unknown"
    if lang_counts:
        primary_language = max(lang_counts.items(), key=lambda item: item[1])[0]

    # --- Framework Detection via content grepping ---
    frameworks: list[str] = []
    framework_scores: dict[str, int] = {}

    for fw_name, indicators in STACK_INDICATORS["frameworks"].items():
        score = 0
        for indicator in indicators:
            # Check filenames
            for fname in filenames:
                if indicator.lower() in fname.lower():
                    score += 1
            # Check file contents (sample up to 20 files for performance)
            for f in all_files[:20]:
                content = read_file_safe(f, max_size_mb=1)
                if content and indicator in content:
                    score += 2
        if score > 0:
            framework_scores[fw_name] = score

    # Sort frameworks by score, take top 5
    sorted_frameworks = sorted(framework_scores.items(), key=lambda x: x[1], reverse=True)
    frameworks = [name for name, _score in sorted_frameworks[:5]]

    # --- Database Detection ---
    database: str | None = None
    db_scores: dict[str, int] = {}
    for db_name, indicators in STACK_INDICATORS["databases"].items():
        score = 0
        for indicator in indicators:
            for fname in filenames:
                if indicator.lower() in fname.lower():
                    score += 1
            for f in all_files[:20]:
                content = read_file_safe(f, max_size_mb=1)
                if content and indicator in content:
                    score += 2
        if score > 0:
            db_scores[db_name] = score

    if db_scores:
        database = max(db_scores.items(), key=lambda item: item[1])[0]

    # --- Build Tool Detection ---
    build_tool: str | None = None
    build_scores: dict[str, int] = {}
    for tool_name, indicators in STACK_INDICATORS["build_tools"].items():
        score = 0
        for indicator in indicators:
            for rpath in rel_paths:
                if indicator.lower() in rpath.lower():
                    score += 3  # Manifest files are strong signals
        if score > 0:
            build_scores[tool_name] = score

    if build_scores:
        build_tool = max(build_scores.items(), key=lambda item: item[1])[0]

    # --- Container Detection ---
    container = ContainerInfo(detected=False)
    containers: list[ContainerInfo] = []
    dockerfile_files = [p for p in rel_paths if _is_dockerfile_path(p)]
    compose_files = [p for p in rel_paths if "docker-compose" in p.lower() or "compose.yaml" in p.lower()]

    if dockerfile_files:
        for df_path in dockerfile_files:
            df_container = ContainerInfo(detected=True, dockerfile_path=df_path)
            df_content = read_file_safe(repo_path / df_path, max_size_mb=1)
            if df_content:
                df_container.dockerfile_content = df_content
                match = re.search(r"^FROM\s+(\S+)", df_content, re.MULTILINE | re.IGNORECASE)
                if match:
                    df_container.base_image = match.group(1)
            containers.append(df_container)
        if compose_files:
            for c in containers:
                c.compose_detected = True
        # Multi-container apps: the first container is the ALB primary, so it
        # must be the public entrypoint, not whatever sorts first on disk. Use
        # docker-compose host port mappings to find it (falling back to a
        # name-based heuristic, then scan order). Ordering here flows into the
        # singular ``container`` alias and the InfraCost primary image.
        primary_path = _primary_dockerfile_from_compose(
            repo_path, compose_files, dockerfile_files
        )
        if primary_path:
            for i, c in enumerate(containers):
                if c.dockerfile_path == primary_path:
                    containers.insert(0, containers.pop(i))
                    logger.info(
                        "Primary container selected from compose: %s", primary_path
                    )
                    break
        container = containers[0]

    # --- Confidence Calculation ---
    # Confidence is a heuristic based on how many signals we found
    signal_count = sum(1 for v in [primary_language, frameworks, database, build_tool, container.detected] if v)
    confidence = min(0.95, 0.3 + (signal_count / 5) * 0.7)

    # Detected files that contributed
    detected_files: list[str] = []
    if primary_language != "unknown":
        # Add representative files for the primary language
        for rpath in rel_paths:
            ext = Path(rpath).suffix.lower()
            if ext in LANGUAGE_EXTENSIONS and LANGUAGE_EXTENSIONS[ext] == primary_language:
                detected_files.append(rpath)
                if len(detected_files) >= 5:
                    break
    if dockerfile_files:
        detected_files.extend(dockerfile_files[:5])
    if compose_files:
        detected_files.extend(compose_files[:1])

    result = StackDetection(
        primary_language=primary_language,
        languages=list(lang_counts.keys()) if lang_counts else [],
        frameworks=frameworks,
        database=database,
        build_tool=build_tool,
        container=container,
        containers=containers,
        confidence=round(confidence, 2),
        detected_files=detected_files[:10],
    )

    logger.info(
        "Stack detection complete: lang=%s, frameworks=%s, db=%s, build=%s, confidence=%.2f",
        result.primary_language,
        result.frameworks,
        result.database,
        result.build_tool,
        result.confidence,
    )
    return result


def get_language_breakdown(repo_path: Path) -> dict[str, int]:
    """Compute approximate LOC per language for repo_metadata."""
    breakdown: dict[str, int] = {}
    for f in Path(repo_path).rglob("*"):
        if f.is_file():
            ext = f.suffix.lower()
            if ext in LANGUAGE_EXTENSIONS:
                lang = LANGUAGE_EXTENSIONS[ext]
                content = read_file_safe(f, max_size_mb=1)
                if content:
                    loc = sum(1 for line in content.splitlines() if line.strip())
                    breakdown[lang] = breakdown.get(lang, 0) + loc
    return breakdown