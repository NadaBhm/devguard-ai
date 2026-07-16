"""Tests for core.input_validator."""

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from core.input_validator import (
    InvalidStatusError,
    LowConfidenceError,
    MalformedInputError,
    MissingStackDetectionError,
    validate_input,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

VALID_FIXTURES = [
    ("sample_input.json", "550e8400-e29b-41d4-a716-446655440000"),
    ("sample_input_variant_lambda_candidate.json", "job-variant-001"),
    ("sample_input_variant_node_ecs.json", "job-variant-002"),
]


def _load_fixture(filename: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


@pytest.mark.parametrize("filename,expected_job_id", VALID_FIXTURES)
def test_valid_fixtures_are_accepted(filename: str, expected_job_id: str) -> None:
    raw = _load_fixture(filename)
    parsed = validate_input(raw)
    assert parsed.job_id == expected_job_id
    assert parsed.status == "completed"
    assert parsed.stack_detection.confidence >= 0.5


def test_confidence_exactly_at_threshold_is_accepted() -> None:
    raw = _load_fixture("sample_input.json")
    raw["stack_detection"]["confidence"] = 0.5
    parsed = validate_input(raw)
    assert parsed.stack_detection.confidence == 0.5


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_missing_optional_field_logs_warning_but_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = _load_fixture("sample_input.json")
    del raw["stack_detection"]["container"]["compose_detected"]
    del raw["security_score"]

    with caplog.at_level(logging.WARNING, logger="core.input_validator"):
        parsed = validate_input(raw)

    assert parsed.stack_detection.container.compose_detected is False
    assert parsed.security_score is None
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("compose_detected" in w for w in warnings)
    assert any("security_score" in w for w in warnings)


def test_explicit_null_optional_field_does_not_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = _load_fixture("sample_input_variant_lambda_candidate.json")
    assert raw["stack_detection"]["database"] is None  # already explicit null

    with caplog.at_level(logging.WARNING, logger="core.input_validator"):
        validate_input(raw)

    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("database" in w for w in warnings)


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_low_confidence_is_rejected() -> None:
    raw = _load_fixture("sample_input_variant_low_confidence.json")
    with pytest.raises(LowConfidenceError) as excinfo:
        validate_input(raw)
    assert excinfo.value.job_id == "job-variant-003"


def test_status_not_completed_is_rejected() -> None:
    raw = _load_fixture("sample_input.json")
    raw["status"] = "processing"
    with pytest.raises(InvalidStatusError):
        validate_input(raw)


def test_missing_status_is_rejected() -> None:
    raw = _load_fixture("sample_input.json")
    del raw["status"]
    with pytest.raises(InvalidStatusError):
        validate_input(raw)


def test_missing_stack_detection_is_rejected() -> None:
    raw = _load_fixture("sample_input.json")
    del raw["stack_detection"]
    with pytest.raises(MissingStackDetectionError):
        validate_input(raw)


def test_null_stack_detection_is_rejected() -> None:
    raw = _load_fixture("sample_input.json")
    raw["stack_detection"] = None
    with pytest.raises(MissingStackDetectionError):
        validate_input(raw)


def test_malformed_repo_metadata_is_rejected() -> None:
    raw = _load_fixture("sample_input.json")
    del raw["repo_metadata"]["loc"]
    with pytest.raises(MalformedInputError):
        validate_input(raw)


def test_negative_confidence_is_rejected_as_malformed() -> None:
    raw = _load_fixture("sample_input.json")
    raw["stack_detection"]["confidence"] = -0.1
    with pytest.raises(MalformedInputError):
        validate_input(raw)


def test_fixtures_are_not_mutated_by_validation() -> None:
    raw = _load_fixture("sample_input.json")
    snapshot = copy.deepcopy(raw)
    validate_input(raw)
    assert raw == snapshot



