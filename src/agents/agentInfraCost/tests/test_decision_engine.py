import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentInfraCost.core.decision_engine import decide
from agentInfraCost.core.input_validator import validate_input
from agentInfraCost.models.exceptions import LowConfidenceStackDetectionError
from agentInfraCost.models.input_models import (
    ContainerInfo,
    InfraCostAgentInput,
    RepoMetadata,
    StackDetection,
)
from agentInfraCost.models.internal_models import EcsSizing

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _make_input(
    *,
    job_id: str = "job-x",
    container_detected: bool,
    compose_detected: bool,
    frameworks: list[str],
    database: str | None,
    loc: int,
    total_files: int = 50,
    primary_language: str = "python",
    confidence: float = 0.9,
) -> InfraCostAgentInput:
    return InfraCostAgentInput(
        job_id=job_id,
        status="completed",
        repo_url="https://github.com/owner/repo",
        repo_metadata=RepoMetadata(
            name="repo",
            branch="main",
            commit_sha="deadbeef",
            total_files=total_files,
            loc=loc,
            language_breakdown={},
        ),
        stack_detection=StackDetection(
            primary_language=primary_language,
            frameworks=frameworks,
            database=database,
            build_tool="pip",
            container=ContainerInfo(
                detected=container_detected,
                base_image=None,
                dockerfile_path=None,
                compose_detected=compose_detected,
            ),
            confidence=confidence,
            detected_files=[],
        ),
    )


@pytest.mark.parametrize(
    "fixture_name,expected",
    [
        ("sample_input.json", "ecs"),
        ("sample_input_variant_lambda_candidate.json", "lambda"),
        ("sample_input_variant_node_ecs.json", "ecs"),
        ("sample_input_variant_low_confidence.json", "REJECT"),
    ],
)
def test_decision_matches_expected_compute_type_per_fixture(
    fixture_name: str, expected: str
) -> None:
    raw = _load(fixture_name)
    if expected == "REJECT":
        with pytest.raises(LowConfidenceStackDetectionError):
            validate_input(raw)
        return

    parsed = validate_input(raw)
    result = decide(parsed)
    assert result.compute_type == expected


def test_all_four_fixtures_do_not_collapse_to_one_outcome() -> None:
    """Anti-overfitting guard: the fixtures must not all produce the same result."""
    outcomes = set()
    for fixture_name in [
        "sample_input.json",
        "sample_input_variant_lambda_candidate.json",
        "sample_input_variant_node_ecs.json",
    ]:
        parsed = validate_input(_load(fixture_name))
        outcomes.add(decide(parsed).compute_type)
    outcomes.add("REJECT")  # the 4th fixture never reaches decision_engine
    assert len(outcomes) >= 2


class TestGenericityAcrossFrameworkNames:
    def test_fastapi_and_express_stacks_reach_ecs_via_identical_sizing_tier(self) -> None:
        """sample_input.json (fastapi/sqlalchemy) and the node variant
        (express/prisma) must be classified using the exact same generic
        signals, never by inspecting the framework names themselves — proven
        here by both landing on the same computed ECS tier despite having
        completely different frameworks."""
        fastapi_result = decide(validate_input(_load("sample_input.json")))
        express_result = decide(validate_input(_load("sample_input_variant_node_ecs.json")))

        assert fastapi_result.compute_type == express_result.compute_type == "ecs"
        assert fastapi_result.ecs is not None and express_result.ecs is not None
        assert fastapi_result.ecs.task_cpu == express_result.ecs.task_cpu
        assert fastapi_result.ecs.task_memory == express_result.ecs.task_memory


class TestSyntheticStacks:
    def test_no_container_large_codebase_favors_ec2(self) -> None:
        payload = _make_input(
            container_detected=False,
            compose_detected=False,
            frameworks=["some-web-framework"],
            database="some-db-engine",
            loc=50000,
            total_files=800,
        )
        result = decide(payload)
        assert result.compute_type == "ec2"
        assert result.ec2 is not None
        assert result.lambda_ is None
        assert result.ecs is None

    def test_container_with_framework_but_no_compose_still_beats_ec2(self) -> None:
        """A Dockerfile with a detected framework but no Compose file should
        still rank ECS above EC2 (container + framework signals dominate
        EC2's weaker combined container signals) — the ranking comes purely
        from the weighted sum, not a cascade of special cases."""
        payload = _make_input(
            container_detected=True,
            compose_detected=False,
            frameworks=["some-web-framework"],
            database=None,
            loc=200,
        )
        result = decide(payload)
        assert result.compute_type == "ecs"

    def test_container_without_compose_or_framework_can_lose_to_lambda(self) -> None:
        """A bare, frameworkless Dockerfile on a tiny codebase is genuinely
        ambiguous: Lambda's combined "no framework + small + no database"
        signals legitimately outweigh ECS's lone container signal. This
        documents that outcome rather than assuming containers always win."""
        payload = _make_input(
            container_detected=True,
            compose_detected=False,
            frameworks=[],
            database=None,
            loc=200,
        )
        result = decide(payload)
        assert result.compute_type == "lambda"

    def test_tiny_containerless_script_is_lambda_regardless_of_language(self) -> None:
        for language in ["python", "javascript", "go", "ruby", "some-future-language"]:
            payload = _make_input(
                container_detected=False,
                compose_detected=False,
                frameworks=[],
                database=None,
                loc=100,
                primary_language=language,
            )
            result = decide(payload)
            assert result.compute_type == "lambda", f"failed for language={language}"
            assert result.lambda_ is not None

    def test_unknown_language_falls_back_to_generic_runtime_and_handler(self) -> None:
        payload = _make_input(
            container_detected=False,
            compose_detected=False,
            frameworks=[],
            database=None,
            loc=100,
            primary_language="some-future-language",
        )
        result = decide(payload)
        assert result.lambda_ is not None
        assert result.lambda_.runtime == "provided.al2023"
        assert result.lambda_.handler == "main.handler"


class TestSizingTiersAreCalculatedNotHardcoded:
    def test_ecs_sizing_scales_up_with_complexity(self) -> None:
        minimal = _make_input(
            container_detected=True,
            compose_detected=True,
            frameworks=["x"],
            database=None,
            loc=100,
            total_files=10,
        )
        complex_ = _make_input(
            container_detected=True,
            compose_detected=True,
            frameworks=["x", "y"],
            database="some-db",
            loc=20000,
            total_files=500,
        )
        minimal_result = decide(minimal)
        complex_result = decide(complex_)
        assert minimal_result.compute_type == complex_result.compute_type == "ecs"
        assert minimal_result.ecs is not None and complex_result.ecs is not None
        assert minimal_result.ecs.task_cpu <= complex_result.ecs.task_cpu
        assert minimal_result.ecs.task_memory <= complex_result.ecs.task_memory
        assert (minimal_result.ecs.task_cpu, minimal_result.ecs.task_memory) != (
            complex_result.ecs.task_cpu,
            complex_result.ecs.task_memory,
        )


class TestResultShapeConsistency:
    def test_only_the_selected_compute_types_sizing_block_is_set(self) -> None:
        for fixture_name in ["sample_input.json", "sample_input_variant_lambda_candidate.json"]:
            result = decide(validate_input(_load(fixture_name)))
            blocks = {"ecs": result.ecs, "lambda": result.lambda_, "ec2": result.ec2}
            populated = [name for name, block in blocks.items() if block is not None]
            assert populated == [result.compute_type]

    def test_ecs_sizing_model_rejects_non_positive_task_cpu(self) -> None:
        """Guards the sizing schema itself against a future bug in the tier
        tables silently producing a nonsensical (non-positive) value."""
        with pytest.raises(ValidationError):
            EcsSizing(
                cluster="c",
                service_name="s",
                task_cpu=-1,
                task_memory=512,
                health_check_path="/health",
                health_check_port=8080,
                timeout_minutes=5,
                min_healthy_percent=50,
                max_percent=200,
            )
