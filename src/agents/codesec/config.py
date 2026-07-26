"""
CodeSec Agent - Configuration
===============================
Centralized configuration for all scanners, scoring weights, severity mappings,
and tool paths.  Environment-aware with sensible defaults.

Design Decisions (ADR-style):
- Scoring weights favor exploitability (SAST > Secrets > Dependencies > Dockerfile > SBOM > Stack)
- Severity multipliers align with CVSS v3.1 qualitative severity ratings
- Tool paths are overridable via env vars for CI/CD flexibility
- All thresholds are configurable to allow tuning without code changes

Author: Nada 
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final


# ---------------------------------------------------------------------------
# Severity Multipliers (CVSS v3.1 aligned)
# ---------------------------------------------------------------------------
# Used by the scorer to weight findings by severity.
SEVERITY_MULTIPLIERS: Final[dict[str, float]] = {
    "critical": 10.0,
    "high": 7.5,
    "medium": 4.0,
    "low": 1.5,
    "info": 0.0,
}

# Reverse mapping for grade thresholds
GRADE_THRESHOLDS: Final[list[tuple[int, str]]] = [
    (90, "A"),
    (80, "B"),
    (70, "C"),
    (60, "D"),
    (50, "E"),
    (0, "F"),
]


# ---------------------------------------------------------------------------
# Scoring Weights (must sum to 100)
# ---------------------------------------------------------------------------
# Rationale:
#   SAST (25):  Direct code vulnerabilities = highest exploitability risk
#   Secrets (20): Hardcoded creds = immediate breach vector
#   Dependencies (20): Known CVEs = easy exploit path
#   Dockerfile (15): Container misconfig = runtime risk
#   SBOM (10):     Visibility matters but is passive
#   Stack Detection (10): Accuracy enables correct downstream infra
SCORING_WEIGHTS: Final[dict[str, int]] = {
    "sast": 25,
    "secrets": 20,
    "dependencies": 20,
    "dockerfile": 15,
    "sbom": 10,
    "stack_detection": 10,
}

# Validate weights sum to 100
assert sum(SCORING_WEIGHTS.values()) == 100, "Scoring weights must sum to 100"


# ---------------------------------------------------------------------------
# Penalty Curves (exponential decay per finding count)
# ---------------------------------------------------------------------------
# Base penalty per finding severity, multiplied by count with decay.
PENALTY_BASE: Final[dict[str, float]] = {
    "critical": 15.0,
    "high": 8.0,
    "medium": 4.0,
    "low": 1.0,
    "info": 0.0,
}

# Decay factor: each additional finding of same severity contributes less
PENALTY_DECAY: Final[float] = 0.85


# ---------------------------------------------------------------------------
# Tool Configurations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolConfig:
    """Configuration for an external security scanning tool."""

    name: str
    executable: str
    version_flag: str = "--version"
    timeout_seconds: int = 300
    enabled: bool = True


# Tool registry — paths overridable via environment variables
TOOLS: Final[dict[str, ToolConfig]] = {
    "semgrep": ToolConfig(
        name="Semgrep",
        executable=os.getenv("SEMGREP_PATH", "semgrep"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("SEMGREP_TIMEOUT", "300")),
        enabled=os.getenv("SEMGREP_ENABLED", "true").lower() == "true",
    ),
    "bandit": ToolConfig(
        name="Bandit",
        executable=os.getenv("BANDIT_PATH", "bandit"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("BANDIT_TIMEOUT", "120")),
        enabled=os.getenv("BANDIT_ENABLED", "true").lower() == "true",
    ),
    "gitleaks": ToolConfig(
        name="GitLeaks",
        executable=os.getenv("GITLEAKS_PATH", "gitleaks"),
        version_flag="version",
        timeout_seconds=int(os.getenv("GITLEAKS_TIMEOUT", "180")),
        enabled=os.getenv("GITLEAKS_ENABLED", "true").lower() == "true",
    ),
    "trufflehog": ToolConfig(
        name="TruffleHog",
        executable=os.getenv("TRUFFLEHOG_PATH", "trufflehog"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("TRUFFLEHOG_TIMEOUT", "180")),
        enabled=os.getenv("TRUFFLEHOG_ENABLED", "false").lower() == "true",
    ),
    "trivy": ToolConfig(
        name="Trivy",
        executable=os.getenv("TRIVY_PATH", "trivy"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("TRIVY_TIMEOUT", "300")),
        enabled=os.getenv("TRIVY_ENABLED", "true").lower() == "true",
    ),
    "safety": ToolConfig(
        name="Safety",
        executable=os.getenv("SAFETY_PATH", "safety"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("SAFETY_TIMEOUT", "120")),
        enabled=os.getenv("SAFETY_ENABLED", "true").lower() == "true",
    ),
    "pip_audit": ToolConfig(
        name="pip-audit",
        executable=os.getenv("PIP_AUDIT_PATH", "pip-audit"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("PIP_AUDIT_TIMEOUT", "120")),
        enabled=os.getenv("PIP_AUDIT_ENABLED", "true").lower() == "true",
    ),
    "hadolint": ToolConfig(
        name="Hadolint",
        executable=os.getenv("HADOLINT_PATH", "hadolint"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("HADOLINT_TIMEOUT", "60")),
        enabled=os.getenv("HADOLINT_ENABLED", "true").lower() == "true",
    ),
    "checkov": ToolConfig(
        name="Checkov",
        executable=os.getenv("CHECKOV_PATH", "checkov"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("CHECKOV_TIMEOUT", "180")),
        enabled=os.getenv("CHECKOV_ENABLED", "false").lower() == "true",
    ),
    "cyclonedx": ToolConfig(
        name="CycloneDX",
        executable=os.getenv("CYCLONEDX_PATH", "cyclonedx-py"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("CYCLONEDX_TIMEOUT", "120")),
        enabled=os.getenv("CYCLONEDX_ENABLED", "true").lower() == "true",
    ),
    "syft": ToolConfig(
        name="Syft",
        executable=os.getenv("SYFT_PATH", "syft"),
        version_flag="--version",
        timeout_seconds=int(os.getenv("SYFT_TIMEOUT", "120")),
        enabled=os.getenv("SYFT_ENABLED", "false").lower() == "true",
    ),
}


# ---------------------------------------------------------------------------
# Scanner-Specific Configurations
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScannerConfig:
    """Per-scanner tunable parameters."""

    max_file_size_mb: int = 10
    exclude_patterns: tuple[str, ...] = field(default_factory=tuple)
    include_patterns: tuple[str, ...] = field(default_factory=tuple)
    parallel_workers: int = 4


SCANNER_CONFIGS: Final[dict[str, ScannerConfig]] = {
    "stack_detection": ScannerConfig(
        max_file_size_mb=5,
        exclude_patterns=("*.min.js", "*.bundle.js", "node_modules/", ".git/"),
        include_patterns=("*",),
        parallel_workers=4,
    ),
    "sast": ScannerConfig(
        max_file_size_mb=10,
        exclude_patterns=("test_", "tests/", "*_test.py", "conftest.py", "venv/", ".venv/"),
        include_patterns=("*.py", "*.js", "*.ts", "*.go", "*.java", "*.rb", "*.php"),
        parallel_workers=4,
    ),
    "secrets": ScannerConfig(
        max_file_size_mb=5,
        exclude_patterns=("*.lock", "package-lock.json", "yarn.lock", "poetry.lock"),
        include_patterns=("*",),
        parallel_workers=2,
    ),
    "dependencies": ScannerConfig(
        max_file_size_mb=1,
        exclude_patterns=(),
        include_patterns=(
            "requirements*.txt",
            "Pipfile",
            "poetry.lock",
            "package.json",
            "package-lock.json",
            "yarn.lock",
            "go.mod",
            "go.sum",
            "pom.xml",
            "build.gradle",
            "Gemfile",
            "Gemfile.lock",
            "Cargo.toml",
            "Cargo.lock",
        ),
        parallel_workers=2,
    ),
    "dockerfile": ScannerConfig(
        max_file_size_mb=1,
        exclude_patterns=(),
        include_patterns=("Dockerfile*", "*.dockerfile", "docker-compose*.yml", "docker-compose*.yaml"),
        parallel_workers=2,
    ),
    "sbom": ScannerConfig(
        max_file_size_mb=1,
        exclude_patterns=(),
        include_patterns=(
            "requirements*.txt",
            "Pipfile",
            "package.json",
            "go.mod",
            "pom.xml",
            "build.gradle",
        ),
        parallel_workers=2,
    ),
}


# ---------------------------------------------------------------------------
# Stack Detection Heuristics
# ---------------------------------------------------------------------------
# Maps filename patterns to technology indicators.
STACK_INDICATORS: Final[dict[str, dict[str, list[str]]]] = {
    "languages": {
        "python": ["*.py", "requirements.txt", "Pipfile", "pyproject.toml", "setup.py"],
        "javascript": ["*.js", "package.json", "*.jsx"],
        "typescript": ["*.ts", "*.tsx", "tsconfig.json"],
        "go": ["*.go", "go.mod"],
        "java": ["*.java", "pom.xml", "build.gradle"],
        "ruby": ["*.rb", "Gemfile"],
        "php": ["*.php", "composer.json"],
        "rust": ["*.rs", "Cargo.toml"],
        "dockerfile": ["Dockerfile", "docker-compose.yml"],
        "terraform": ["*.tf", "*.tfvars"],
    },
    "frameworks": {
        "fastapi": ["fastapi", "FastAPI"],
        "flask": ["flask", "Flask"],
        "django": ["django", "Django"],
        "express": ["express", "Express"],
        "nestjs": ["@nestjs"],
        "react": ["react", "React"],
        "vue": ["vue", "Vue"],
        "angular": ["@angular"],
        "spring": ["spring-boot", "spring-boot-starter"],
        "gin": ["github.com/gin-gonic/gin"],
        "rails": ["rails", "Ruby on Rails"],
        "laravel": ["laravel/framework"],
        "sqlalchemy": ["sqlalchemy", "SQLAlchemy"],
        "mongoose": ["mongoose"],
        "prisma": ["prisma"],
    },
    "databases": {
        "postgresql": ["postgresql", "psycopg", "pg", "postgres"],
        "mysql": ["mysql", "pymysql", "mysql-connector"],
        "mongodb": ["mongodb", "pymongo", "mongoose"],
        "redis": ["redis", "redis-py"],
        "sqlite": ["sqlite"],
        "dynamodb": ["boto3", "dynamodb"],
    },
    "build_tools": {
        "pip": ["requirements.txt", "setup.py", "pyproject.toml"],
        "poetry": ["poetry.lock", "pyproject.toml"],
        "npm": ["package.json", "package-lock.json"],
        "yarn": ["yarn.lock"],
        "pnpm": ["pnpm-lock.yaml"],
        "maven": ["pom.xml"],
        "gradle": ["build.gradle"],
        "go_modules": ["go.mod"],
        "cargo": ["Cargo.toml"],
    },
}


# ---------------------------------------------------------------------------
# OWASP Top 10 2021 → CWE Mapping (for SAST categorization)
# ---------------------------------------------------------------------------
OWASP_CWE_MAP: Final[dict[str, list[str]]] = {
    "A01:2021 – Broken Access Control": ["CWE-22", "CWE-284", "CWE-285", "CWE-639"],
    "A02:2021 – Cryptographic Failures": ["CWE-261", "CWE-296", "CWE-310", "CWE-319", "CWE-326", "CWE-327", "CWE-328", "CWE-330", "CWE-331", "CWE-335", "CWE-338", "CWE-345", "CWE-347", "CWE-523", "CWE-720", "CWE-757", "CWE-759", "CWE-760", "CWE-780", "CWE-916"],
    "A03:2021 – Injection": ["CWE-77", "CWE-78", "CWE-79", "CWE-88", "CWE-89", "CWE-90", "CWE-91", "CWE-564", "CWE-917", "CWE-943"],
    "A04:2021 – Insecure Design": ["CWE-73", "CWE-183", "CWE-209", "CWE-213", "CWE-235", "CWE-256", "CWE-257", "CWE-266", "CWE-269", "CWE-280", "CWE-311", "CWE-312", "CWE-313", "CWE-316", "CWE-419", "CWE-430", "CWE-434", "CWE-444", "CWE-451", "CWE-472", "CWE-501", "CWE-522", "CWE-525", "CWE-539", "CWE-579", "CWE-598", "CWE-602", "CWE-642", "CWE-668", "CWE-669", "CWE-670", "CWE-671", "CWE-672", "CWE-673", "CWE-674", "CWE-675", "CWE-676", "CWE-681", "CWE-693", "CWE-697", "CWE-698", "CWE-710", "CWE-711", "CWE-732", "CWE-733", "CWE-749", "CWE-759", "CWE-760", "CWE-827", "CWE-840", "CWE-841", "CWE-918", "CWE-933", "CWE-1004", "CWE-1031", "CWE-1173", "CWE-1174", "CWE-1175", "CWE-1176", "CWE-1177", "CWE-1188", "CWE-1231", "CWE-1232", "CWE-1233", "CWE-1234", "CWE-1235", "CWE-1236", "CWE-1237", "CWE-1238", "CWE-1239", "CWE-1240", "CWE-1241", "CWE-1242", "CWE-1243", "CWE-1244", "CWE-1245", "CWE-1246", "CWE-1247", "CWE-1248", "CWE-1249", "CWE-1250", "CWE-1251", "CWE-1252", "CWE-1253", "CWE-1254", "CWE-1255", "CWE-1256", "CWE-1257", "CWE-1258", "CWE-1259", "CWE-1260", "CWE-1261", "CWE-1262", "CWE-1263", "CWE-1264", "CWE-1265", "CWE-1266", "CWE-1267", "CWE-1268", "CWE-1269", "CWE-1270", "CWE-1271", "CWE-1272", "CWE-1273", "CWE-1274", "CWE-1275"],
    "A05:2021 – Security Misconfiguration": ["CWE-2", "CWE-11", "CWE-13", "CWE-15", "CWE-16", "CWE-260", "CWE-315", "CWE-520", "CWE-526", "CWE-537", "CWE-541", "CWE-547", "CWE-611", "CWE-614", "CWE-756", "CWE-776", "CWE-942", "CWE-1004", "CWE-1032", "CWE-1174"],
    "A06:2021 – Vulnerable and Outdated Components": ["CWE-937", "CWE-1035", "CWE-1104"],
    "A07:2021 – Identification and Authentication Failures": ["CWE-287", "CWE-288", "CWE-290", "CWE-294", "CWE-295", "CWE-297", "CWE-300", "CWE-302", "CWE-304", "CWE-306", "CWE-307", "CWE-346", "CWE-384", "CWE-521", "CWE-613", "CWE-620", "CWE-640", "CWE-798", "CWE-940", "CWE-1216"],
    "A08:2021 – Software and Data Integrity Failures": ["CWE-345", "CWE-353", "CWE-426", "CWE-494", "CWE-502", "CWE-565", "CWE-784", "CWE-829", "CWE-830", "CWE-915"],
    "A09:2021 – Security Logging and Monitoring Failures": ["CWE-117", "CWE-223", "CWE-532", "CWE-778"],
    "A10:2021 – Server-Side Request Forgery (SSRF)": ["CWE-918"],
}


# ---------------------------------------------------------------------------
# Global Defaults
# ---------------------------------------------------------------------------

DEFAULT_CLONE_DIR: Final[str] = os.getenv("CODESEC_CLONE_DIR", "/tmp/codesec-clones")
MAX_REPO_SIZE_MB: Final[int] = int(os.getenv("CODESEC_MAX_REPO_SIZE_MB", "500"))
MAX_FILES_PER_REPO: Final[int] = int(os.getenv("CODESEC_MAX_FILES", "10000"))
GITHUB_URL_PATTERN: Final[str] = r"^https?://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+(/.*)?$"


def get_tool_config(tool_name: str) -> ToolConfig:
    """Retrieve configuration for a named tool."""
    if tool_name not in TOOLS:
        raise ValueError(f"Unknown tool: {tool_name}. Available: {list(TOOLS.keys())}")
    return TOOLS[tool_name]


def get_scanner_config(scanner_name: str) -> ScannerConfig:
    """Retrieve configuration for a named scanner."""
    if scanner_name not in SCANNER_CONFIGS:
        raise ValueError(f"Unknown scanner: {scanner_name}")
    return SCANNER_CONFIGS[scanner_name]