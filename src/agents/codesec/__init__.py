
"""CodeSec Agent — Security analysis for public GitHub/GitLab repos."""

from __future__ import annotations

from .agent import CodeSecAgent
from .models import CodeSecResult

__version__ = "1.0.0"
__all__ = ["CodeSecAgent", "CodeSecResult"]