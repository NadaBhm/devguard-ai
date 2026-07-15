
"""
CodeSec Agent — Subgroup 1 Security Analysis Agent
====================================================
Part of the DevGuard AI platform. Analyzes public GitHub repositories for:
- Technology stack detection
- Static Application Security Testing (SAST / OWASP Top 10)
- Hardcoded secrets detection
- Dependency vulnerability scanning (CVEs)
- Dockerfile security best practices
- SBOM generation (CycloneDX)
- Security scoring (0-100, grade A-F)

Usage:
    from src.subgroup1.codesec import CodeSecAgent
    agent = CodeSecAgent()
    result = await agent.analyze("https://github.com/owner/repo")

Author: Nada 
"""

from __future__ import annotations

from .agent import CodeSecAgent
from .models import CodeSecResult

__version__ = "1.0.0"
__all__ = ["CodeSecAgent", "CodeSecResult"]