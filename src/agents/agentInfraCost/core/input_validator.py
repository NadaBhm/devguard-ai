"""Step 1 of the InfraCost pipeline: validate and parse Agent 1's payload.

Applies structural validation (via ``models.input_schema.RepoAnalysisInput``)
followed by the fail-fast business rules described in the agent's contract:

- ``status`` must be ``"completed"``.
- ``stack_detection`` must be present.
- ``stack_detection.confidence`` must be >= ``MIN_CONFIDENCE`` — below that,
  the stack detection is too uncertain to safely recommend an architecture.

Absence of optional fields (e.g. ``compose_detected``) is logged as a
warning and does not block the pipeline.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from pydantic import ValidationError

from models.input_schema import RepoAnalysisInput

logger = logging.getLogger(__name__)

MIN_CONFIDENCE: Final[float] = 0.5
REQUIRED_STATUS: Final[str] = "completed"

# Dotted paths of schema-optional fields whose absence from the *raw*
# payload is worth flagging, even though a default will be used.
_OPTIONAL_FIELD_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("stack_detection", "container", "compose_detected"),
    ("stack_detection", "database"),
    ("stack_detection", "build_tool"),
    ("security_score",),
)


class InputValidationError(Exception):
    """Base class for every fail-fast rejection of Agent 1's payload."""

    def __init__(self, message: str, *, job_id: str | None = None) -> None:
        self.job_id = job_id
        super().__init__(message)


class InvalidStatusError(InputValidationError):
    """``status`` is not ``"completed"``."""


class MissingStackDetectionError(InputValidationError):
    """``stack_detection`` is absent from the payload."""


class LowConfidenceError(InputValidationError):
    """``stack_detection.confidence`` is below ``MIN_CONFIDENCE``."""


class MalformedInputError(InputValidationError):
    """The payload does not match the expected structural schema."""


def _field_present(raw: dict[str, Any], path: tuple[str, ...]) -> bool:
    """Return True if every key in ``path`` exists in ``raw`` (value may be None)."""
    node: Any = raw
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def _warn_missing_optional_fields(raw: dict[str, Any], job_id: str) -> None:
    """Log a warning for each optional field missing from the raw payload."""
    for path in _OPTIONAL_FIELD_PATHS:
        if not _field_present(raw, path):
            logger.warning(
                "job_id=%s optional field '%s' missing from input payload; "
                "continuing with schema default",
                job_id,
                ".".join(path),
            )


def validate_input(raw: dict[str, Any]) -> RepoAnalysisInput:
    """Validate and parse a raw payload from the repo-analysis agent.

    Args:
        raw: The decoded JSON payload produced by Agent 1.

    Returns:
        The parsed and validated ``RepoAnalysisInput``.

    Raises:
        InvalidStatusError: ``status`` is not ``"completed"``.
        MissingStackDetectionError: ``stack_detection`` is absent.
        MalformedInputError: the payload fails structural schema validation.
        LowConfidenceError: ``stack_detection.confidence`` is below
            ``MIN_CONFIDENCE``.
    """
    job_id = str(raw.get("job_id", "<unknown>"))

    status = raw.get("status")
    if status != REQUIRED_STATUS:
        raise InvalidStatusError(
            f"Expected status='{REQUIRED_STATUS}', got status={status!r}",
            job_id=job_id,
        )

    if not _field_present(raw, ("stack_detection",)) or raw.get("stack_detection") is None:
        raise MissingStackDetectionError(
            "Payload is missing 'stack_detection'; cannot recommend an "
            "architecture without a detected stack.",
            job_id=job_id,
        )

    try:
        parsed = RepoAnalysisInput.model_validate(raw)
    except ValidationError as exc:
        raise MalformedInputError(
            f"Payload failed schema validation: {exc}", job_id=job_id
        ) from exc

    if parsed.stack_detection.confidence < MIN_CONFIDENCE:
        raise LowConfidenceError(
            f"stack_detection.confidence={parsed.stack_detection.confidence} is "
            f"below the minimum usable threshold of {MIN_CONFIDENCE}; stack "
            "detection is too uncertain to recommend an architecture.",
            job_id=job_id,
        )

    _warn_missing_optional_fields(raw, job_id)

    return parsed
