import json
from pathlib import Path

import pytest

from agentInfraCost.core.finops_optimizer import optimize
from agentInfraCost.core.input_validator import validate_input
from agentInfraCost.models.input_models import (
    ContainerInfo,
    InfraCostAgentInput,
    RepoMetadata,
    StackDetection,
)
from agentInfraCost.models.internal_models import (
    DecisionResult,
    Ec2Sizing,
    EcsSizing,
    LambdaSizing,
    ScoreBreakdown,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _score_breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(ecs_score=0.0, lambda_score=0.0, ec2_score=0.0, signals={})


def _make_input(
    *,
    container_detected: bool,
    compose_detected: bool,
    database: str | None,
    loc: int,
) -> InfraCostAgentInput:
    return InfraCostAgentInput(
        job_id="job-x",
        status="completed",
        repo_url="https://github.com/owner/repo",
        repo_metadata=RepoMetadata(
            name="repo", branch="main", commit_sha="deadbeef", total_files=50, loc=loc,
            language_breakdown={},
        ),
        stack_detection=StackDetection(
            primary_language="python",
            frameworks=[],
            database=database,
            build_tool="pip",
            container=ContainerInfo(
                detected=container_detected,
                base_image=None,
                dockerfile_path=None,
                compose_detected=compose_detected,
            ),
            confidence=0.9,
            detected_files=[],
        ),
    )


def _ecs_decision() -> DecisionResult:
    return DecisionResult(
        compute_type="ecs",
        ecs=EcsSizing(
            cluster="devguard-cluster", service_name="svc", task_cpu=512, task_memory=1024,
            health_check_path="/health", health_check_port=8080, timeout_minutes=5,
            min_healthy_percent=50, max_percent=200,
        ),
        score_breakdown=_score_breakdown(),
    )


def _lambda_decision() -> DecisionResult:
    return DecisionResult(
        compute_type="lambda",
        lambda_=LambdaSizing(
            function_name="fn", runtime="python3.12", memory_mb=256, timeout_seconds=30,
            handler="main.handler",
        ),
        score_breakdown=_score_breakdown(),
    )


def _ec2_decision() -> DecisionResult:
    return DecisionResult(
        compute_type="ec2",
        ec2=Ec2Sizing(
            instance_type="t3.small", ami_id="ami-0000000000000000", instance_count=1,
            key_pair_name="devguard-keypair", health_check_path="/status",
            health_check_port=80, timeout_minutes=10,
        ),
        score_breakdown=_score_breakdown(),
    )


class TestNominalEcsEc2Rules:
    def test_interruption_tolerant_workload_gets_spot(self) -> None:
        payload = _make_input(
            container_detected=True, compose_detected=False, database=None, loc=500
        )
        result = optimize(payload, _ecs_decision())
        assert result.recommended_option == "spot"
        assert "spot" not in [o.name for o in result.discarded_options]

    def test_critical_large_codebase_gets_reserved(self) -> None:
        payload = _make_input(
            container_detected=True, compose_detected=True, database="some-db", loc=20000
        )
        result = optimize(payload, _ecs_decision())
        assert result.recommended_option == "reserved"

    def test_critical_small_codebase_gets_graviton(self) -> None:
        payload = _make_input(
            container_detected=True, compose_detected=True, database=None, loc=200
        )
        result = optimize(payload, _ecs_decision())
        assert result.recommended_option == "graviton"

    def test_never_recommends_spot_for_compose_without_scaling_evidence(self) -> None:
        """Direct check of the rule called out in the module spec: a
        compose-based topology (no evidence of horizontal scaling in the
        input schema) must never end up with Spot recommended."""
        payload = _make_input(
            container_detected=True, compose_detected=True, database=None, loc=50
        )
        result = optimize(payload, _ecs_decision())
        assert result.recommended_option != "spot"
        spot_option = next(o for o in result.discarded_options if o.name == "spot")
        assert spot_option.recommended is False

    def test_works_identically_for_ec2(self) -> None:
        payload = _make_input(
            container_detected=False, compose_detected=False, database=None, loc=500
        )
        result = optimize(payload, _ec2_decision())
        assert result.recommended_option == "spot"


class TestLambdaRules:
    def test_lambda_always_recommends_graviton(self) -> None:
        for loc, compose, database in [(100, False, None), (50000, True, "some-db")]:
            payload = _make_input(
                container_detected=False, compose_detected=compose, database=database, loc=loc
            )
            result = optimize(payload, _lambda_decision())
            assert result.recommended_option == "graviton"

    def test_lambda_marks_spot_and_reserved_as_not_applicable(self) -> None:
        payload = _make_input(
            container_detected=False, compose_detected=False, database=None, loc=100
        )
        result = optimize(payload, _lambda_decision())
        by_name = {o.name: o for o in result.discarded_options}
        assert "not applicable" in by_name["spot"].reason.lower()
        assert "not applicable" in by_name["reserved"].reason.lower()


class TestResultShape:
    def test_recommended_option_never_appears_in_discarded_options(self) -> None:
        for decision in (_ecs_decision(), _lambda_decision(), _ec2_decision()):
            payload = _make_input(
                container_detected=True, compose_detected=True, database="db", loc=9000
            )
            result = optimize(payload, decision)
            assert result.recommended_option not in [o.name for o in result.discarded_options]

    def test_exactly_three_discarded_options_for_every_compute_type(self) -> None:
        payload = _make_input(
            container_detected=True, compose_detected=False, database=None, loc=500
        )
        for decision in (_ecs_decision(), _lambda_decision(), _ec2_decision()):
            result = optimize(payload, decision)
            assert len(result.discarded_options) == 3

    def test_context_used_carries_the_signals_and_compute_type(self) -> None:
        payload = _make_input(
            container_detected=True, compose_detected=True, database="db", loc=9000
        )
        result = optimize(payload, _ecs_decision())
        assert result.context_used["compute_type"] == "ecs"
        assert result.context_used["critical_stateful"] is True
        assert result.context_used["database_present"] is True


class TestErrors:
    def test_unknown_compute_type_raises_value_error(self) -> None:
        payload = _make_input(
            container_detected=True, compose_detected=True, database=None, loc=500
        )
        decision = _ecs_decision().model_copy(update={"compute_type": "unknown"})
        with pytest.raises(ValueError, match="Unknown compute_type"):
            optimize(payload, decision)


class TestAgainstRealFixtures:
    def test_sample_input_fastapi_compose_db_favors_reserved(self) -> None:
        payload = validate_input(_load("sample_input.json"))
        result = optimize(payload, _ecs_decision())
        assert result.recommended_option == "reserved"

    def test_node_ecs_variant_favors_reserved_too_despite_different_framework(self) -> None:
        """Same generic reasoning path as FastAPI (compose+db+large codebase),
        proving the rule doesn't key on framework names either."""
        payload = validate_input(_load("sample_input_variant_node_ecs.json"))
        result = optimize(payload, _ecs_decision())
        assert result.recommended_option == "reserved"

    def test_lambda_candidate_variant_still_gets_graviton_not_spot(self) -> None:
        payload = validate_input(_load("sample_input_variant_lambda_candidate.json"))
        result = optimize(payload, _lambda_decision())
        assert result.recommended_option == "graviton"
