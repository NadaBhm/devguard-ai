import json
import logging
from pathlib import Path
from typing import Any

import pytest

from agentInfraCost.core.input_validator import (
    CONFIDENCE_THRESHOLD,
    parse_and_validate_input,
    validate_input,
)
from agentInfraCost.models.exceptions import (
    InputValidationError,
    JobNotCompletedError,
    LowConfidenceStackDetectionError,
    MissingStackDetectionError,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


class TestNominal:
    def test_sample_input_parses(self) -> None:
        result = validate_input(_load("sample_input.json"))
        assert result.job_id == "550e8400-e29b-41d4-a716-446655440000"
        assert result.stack_detection.primary_language == "python"
        assert result.stack_detection.container.detected is True

    def test_lambda_candidate_parses(self) -> None:
        result = validate_input(_load("sample_input_variant_lambda_candidate.json"))
        assert result.stack_detection.container.detected is False
        assert result.stack_detection.frameworks == []

    def test_node_ecs_parses(self) -> None:
        result = validate_input(_load("sample_input_variant_node_ecs.json"))
        assert result.stack_detection.primary_language == "javascript"
        assert "express" in result.stack_detection.frameworks


class TestConfidenceGate:
    def test_low_confidence_variant_is_rejected(self) -> None:
        with pytest.raises(LowConfidenceStackDetectionError) as exc_info:
            validate_input(_load("sample_input_variant_low_confidence.json"))
        assert exc_info.value.confidence == pytest.approx(0.31)

    def test_confidence_exactly_at_threshold_passes(self) -> None:
        payload = _load("sample_input.json")
        payload["stack_detection"]["confidence"] = CONFIDENCE_THRESHOLD
        result = validate_input(payload)
        assert result.stack_detection.confidence == CONFIDENCE_THRESHOLD

    def test_confidence_just_below_threshold_is_rejected(self) -> None:
        payload = _load("sample_input.json")
        payload["stack_detection"]["confidence"] = CONFIDENCE_THRESHOLD - 0.01
        with pytest.raises(LowConfidenceStackDetectionError):
            validate_input(payload)


class TestFailFastRules:
    def test_status_not_completed_is_rejected(self) -> None:
        payload = _load("sample_input.json")
        payload["status"] = "failed"
        with pytest.raises(JobNotCompletedError) as exc_info:
            validate_input(payload)
        assert exc_info.value.status == "failed"

    def test_missing_stack_detection_is_rejected(self) -> None:
        payload = _load("sample_input.json")
        del payload["stack_detection"]
        with pytest.raises(MissingStackDetectionError):
            validate_input(payload)

    def test_null_stack_detection_is_rejected(self) -> None:
        payload = _load("sample_input.json")
        payload["stack_detection"] = None
        with pytest.raises(MissingStackDetectionError):
            validate_input(payload)


class TestOtherSchemaErrors:
    def test_missing_required_nested_field_raises_input_validation_error(self) -> None:
        payload = _load("sample_input.json")
        del payload["stack_detection"]["container"]
        with pytest.raises(InputValidationError):
            validate_input(payload)

    def test_non_dict_payload_is_rejected(self) -> None:
        with pytest.raises(InputValidationError):
            validate_input(["not", "a", "dict"])  # type: ignore[arg-type]


class TestOptionalFieldWarnings:
    def test_missing_compose_detected_logs_warning_but_still_succeeds(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = _load("sample_input.json")
        del payload["stack_detection"]["container"]["compose_detected"]
        with caplog.at_level(logging.WARNING):
            result = validate_input(payload)
        assert result.stack_detection.container.compose_detected is None
        assert any(
            "compose_detected" in record.message for record in caplog.records
        )

    def test_no_warning_when_optional_fields_present(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        payload = _load("sample_input.json")
        with caplog.at_level(logging.WARNING):
            validate_input(payload)
        assert len(caplog.records) == 0


class TestParseAndValidateInput:
    def test_valid_json_string(self) -> None:
        raw = (FIXTURES_DIR / "sample_input.json").read_text(encoding="utf-8")
        result = parse_and_validate_input(raw)
        assert result.job_id == "550e8400-e29b-41d4-a716-446655440000"

    def test_malformed_json_raises_input_validation_error(self) -> None:
        with pytest.raises(InputValidationError):
            parse_and_validate_input("{not valid json")
