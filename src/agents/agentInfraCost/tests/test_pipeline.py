import json
from pathlib import Path

import pytest

from agentInfraCost.core import decision_engine
from agentInfraCost.core.approval_manager import InvalidApprovalTransitionError
from agentInfraCost.core.pipeline import apply_approval, run_pipeline
from agentInfraCost.models.exceptions import LowConfidenceStackDetectionError, PipelineStageError

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _no_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


class TestNominalRuns:
    def test_sample_input_produces_ecs_output_with_docker_image(self) -> None:
        output = run_pipeline(_load("sample_input.json"))
        assert output.compute_type == "ecs"
        assert output.aws_config.ecs is not None
        assert output.aws_config.lambda_ is None
        assert output.aws_config.ec2 is None
        assert output.approval.status == "pending"
        assert output.artifacts.docker_image is not None
        assert output.artifacts.docker_image.tag == "a1b2c3d4e5f6"
        assert output.enrichment.enrichment_source == "fallback"

    def test_lambda_candidate_produces_lambda_output_without_docker_image(self) -> None:
        output = run_pipeline(_load("sample_input_variant_lambda_candidate.json"))
        assert output.compute_type == "lambda"
        assert output.aws_config.lambda_ is not None
        assert output.artifacts.docker_image is None
        assert output.artifacts.dockerfile is None

    def test_node_ecs_variant_produces_ecs_output(self) -> None:
        output = run_pipeline(_load("sample_input_variant_node_ecs.json"))
        assert output.compute_type == "ecs"

    def test_output_is_a_fully_valid_infracost_output(self) -> None:
        output = run_pipeline(_load("sample_input.json"))
        dumped = output.model_dump(mode="json", by_alias=True)
        assert dumped["schema_version"] == "1.0"
        assert dumped["job_id"] == "550e8400-e29b-41d4-a716-446655440000"

    def test_terraform_files_are_present_and_non_empty(self) -> None:
        output = run_pipeline(_load("sample_input.json"))
        files = output.artifacts.terraform.files
        assert set(files.keys()) == {"main.tf", "variables.tf", "outputs.tf"}
        for content in files.values():
            assert content.strip()


class TestFailFastPropagatesAsPipelineStageError:
    def test_low_confidence_fixture_fails_at_input_validation_stage(self) -> None:
        with pytest.raises(PipelineStageError) as exc_info:
            run_pipeline(_load("sample_input_variant_low_confidence.json"))
        assert exc_info.value.stage == "input_validation"
        assert isinstance(exc_info.value.original_error, LowConfidenceStackDetectionError)

    def test_a_failure_deep_in_a_later_stage_is_still_named_correctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(payload):
            raise RuntimeError("simulated bug inside decision_engine")

        # pipeline.py does `from . import decision_engine` and calls
        # decision_engine.decide(...), so patching the `decide` attribute on
        # the (shared) module object is what takes effect here.
        monkeypatch.setattr(decision_engine, "decide", _boom)

        with pytest.raises(PipelineStageError) as exc_info:
            run_pipeline(_load("sample_input.json"))
        assert exc_info.value.stage == "decision_engine"
        assert isinstance(exc_info.value.original_error, RuntimeError)


class TestApplyApproval:
    def test_approve_nominal(self) -> None:
        output = run_pipeline(_load("sample_input.json"))
        approved = apply_approval(output, approved=True, approved_by="user@example.com")
        assert approved.approval.status == "approved"
        assert approved.approval.approved_by == "user@example.com"

    def test_reject_nominal(self) -> None:
        output = run_pipeline(_load("sample_input.json"))
        rejected = apply_approval(output, approved=False)
        assert rejected.approval.status == "rejected"
        assert rejected.approval.approved_by is None

    def test_does_not_mutate_the_original_output(self) -> None:
        output = run_pipeline(_load("sample_input.json"))
        apply_approval(output, approved=True, approved_by="user@example.com")
        assert output.approval.status == "pending"

    def test_approving_twice_raises(self) -> None:
        output = run_pipeline(_load("sample_input.json"))
        approved = apply_approval(output, approved=True, approved_by="user@example.com")
        with pytest.raises(InvalidApprovalTransitionError):
            apply_approval(approved, approved=True, approved_by="user@example.com")

    def test_approving_without_approved_by_raises_value_error(self) -> None:
        output = run_pipeline(_load("sample_input.json"))
        with pytest.raises(ValueError, match="approved_by is required"):
            apply_approval(output, approved=True, approved_by=None)

    def test_rejecting_an_already_approved_job_raises(self) -> None:
        output = run_pipeline(_load("sample_input.json"))
        approved = apply_approval(output, approved=True, approved_by="user@example.com")
        with pytest.raises(InvalidApprovalTransitionError):
            apply_approval(approved, approved=False)
