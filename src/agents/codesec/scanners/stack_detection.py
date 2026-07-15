"""
CodeSec Stack Detection Scanner
=================================
Detects primary language, frameworks, database, build tool, and container
information from repository file list and content.

US-1.1.2: As a user, I want to know my project's tech stack so that I
understand its architecture.  Detection accuracy target: >=80% on test repos.

Design Decisions:
- Heuristic-based detection using filename patterns and content grepping.
- No arbitrary code execution — purely static file analysis. citespec-NFR-security
- Confidence score computed from match strength and file coverage.
- Extensible indicator registry in config.py for new technologies.

Author: Nada 
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..config import STACK_INDICATORS
from ..models import ContainerInfo, StackDetection
from . import ScannerError, find_files, read_file_safe

logger = logging.getLogger(__name__)


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


def detect_stack(repo_path: Path) -> StackDetection:
    """
    Analyze a cloned repository and detect its technology stack.

    Args:
        repo_path: Path to the cloned repository root.

    Returns:
        StackDetection model with detected technologies and confidence.

    Raises:
        ScannerError: If repo_path is invalid or unreadable.
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
    dockerfile_files = [p for p in rel_paths if "dockerfile" in p.lower() or p.lower().endswith(".dockerfile")]
    compose_files = [p for p in rel_paths if "docker-compose" in p.lower() or "compose.yaml" in p.lower()]

    if dockerfile_files:
        container.detected = True
        container.dockerfile_path = dockerfile_files[0]
        # Try to extract base image from Dockerfile
        df_content = read_file_safe(repo_path / dockerfile_files[0], max_size_mb=1)
        if df_content:
            match = re.search(r"^FROM\s+(\S+)", df_content, re.MULTILINE | re.IGNORECASE)
            if match:
                container.base_image = match.group(1)
    if compose_files:
        container.compose_detected = True

    # --- Confidence Calculation ---
    # Confidence is a heuristic based on how many signals we found
    signal_count = sum(1 for v in [primary_language, frameworks, database, build_tool, container.detected] if v)
    confidence = min(0.95, 0.3 + (signal_count / 6) * 0.7)

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
        detected_files.extend(dockerfile_files[:1])
    if compose_files:
        detected_files.extend(compose_files[:1])

    result = StackDetection(
        primary_language=primary_language,
        languages=list(lang_counts.keys()) if lang_counts else [],
        frameworks=frameworks,
        database=database,
        build_tool=build_tool,
        container=container,
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
    """
    Compute approximate LOC per language for repo_metadata.

    Returns:
        Dictionary mapping language name to line count.
    """
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