"""Module 1: validates and parses the payload received from Agent 1.

Fail-fast rules (checked before full schema validation, per spec):
  - status must be "completed"
  - stack_detection must be present
  - stack_detection.confidence must be >= CONFIDENCE_THRESHOLD

Any other structural problem (missing required field, wrong type, ...) is
caught by the Pydantic schema and re-raised as a typed InputValidationError.
Missing *optional* fields are logged as warnings and otherwise ignored.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ..models.exceptions import (
    InputValidationError,
    JobNotCompletedError,
    LowConfidenceStackDetectionError,
    MissingStackDetectionError,
)
from ..models.input_models import InfraCostAgentInput

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.5

# Dotted paths (as raw dict key sequences) to optional fields worth warning
# about when entirely absent from the payload, since their absence means
# this agent falls back to conservative defaults downstream.
_OPTIONAL_FIELD_PATHS: tuple[tuple[str, ...], ...] = (
    ("stack_detection", "container", "compose_detected"),
    ("stack_detection", "database"),
    ("stack_detection", "build_tool"),
    ("repo_metadata", "commit_sha"),
    ("security_score",),
)


def _get_nested(payload: dict[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    """Walks `path` into `payload`. Returns (found, value)."""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False, None
        node = node[key]
    return True, node


def _warn_missing_optional_fields(payload: dict[str, Any]) -> None:
    for path in _OPTIONAL_FIELD_PATHS:
        found, _ = _get_nested(payload, path)
        if not found:
            logger.warning(
                "Optional field '%s' is absent from the input payload; "
                "continuing with defaults.",
                ".".join(path),
            )


def validate_input(payload: dict[str, Any]) -> InfraCostAgentInput:
    """Validates and parses a raw dict payload from the repo-analysis agent.

    :raises JobNotCompletedError: status is not "completed"
    :raises MissingStackDetectionError: stack_detection is absent or null
    :raises LowConfidenceStackDetectionError: confidence < CONFIDENCE_THRESHOLD
    :raises InputValidationError: any other schema validation failure
    """
    if not isinstance(payload, dict):
        raise InputValidationError(f"Expected a JSON object, got {type(payload).__name__}")

    status = payload.get("status")
    if status != "completed":
        raise JobNotCompletedError(str(status))

    if payload.get("stack_detection") is None:
        raise MissingStackDetectionError()

    try:
        parsed = InfraCostAgentInput.model_validate(payload)
    except PydanticValidationError as exc:
        raise InputValidationError(f"Input payload failed schema validation: {exc}") from exc

    if parsed.stack_detection.confidence < CONFIDENCE_THRESHOLD:
        raise LowConfidenceStackDetectionError(
            parsed.stack_detection.confidence, CONFIDENCE_THRESHOLD
        )

    _warn_missing_optional_fields(payload)

    return parsed


def parse_and_validate_input(raw_json: str) -> InfraCostAgentInput:
    """Convenience wrapper: parses a raw JSON string then validates it."""
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise InputValidationError(f"Input payload is not valid JSON: {exc}") from exc
    return validate_input(payload)
