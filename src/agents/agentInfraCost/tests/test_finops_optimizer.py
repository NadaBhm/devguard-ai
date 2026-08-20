import json
from pathlib import Path

import pytest

import core.finops_optimizer as finops_optimizer
from core.decision_engine import DecisionResult, decide_architecture
from core.finops_optimizer import _is_spot_safe, optimize_finops
from models.input_schema import RepoAnalysisInput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_analysis(filename: str) -> RepoAnalysisInput:
    raw = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    return RepoAnalysisInput.model_validate(raw)


def _load_raw(filename: str) -> dict:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "filename",
    ["sample_input.json", "sample_input_variant_node_ecs.json"],
)
def test_ecs_with_compose_and_scaling_recommends_spot(filename: str) -> None:
    """Both fixtures have compose_detected=true but DO scale horizontally
    (module 5 grows task_count with load) — so Spot is safe."""
    analysis = _load_analysis(filename)
    decision = decide_architecture(analysis)
    rec = optimize_finops(analysis, decision)

    assert rec.recommended.name == "spot"
    assert rec.context["compose_detected"] is True
    assert rec.context["horizontal_scaling_detected"] is True
    assert "graviton" in [d.name for d in rec.discarded]


def test_ecs_graviton_is_discarded_as_already_priced() -> None:
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    rec = optimize_finops(analysis, decision)

    graviton = next(d for d in rec.discarded if d.name == "graviton")
    assert "déjà" in graviton.reason
    assert rec.context["already_graviton_priced"] is True


def test_lambda_recommends_reserved_concurrency() -> None:
    analysis = _load_analysis("sample_input_variant_lambda_candidate.json")
    decision = decide_architecture(analysis)
    rec = optimize_finops(analysis, decision)

    assert rec.recommended.name == "reserved_concurrency"
    assert rec.context["reserved_concurrency"] == 8
    assert rec.context["peak_monthly_invocations"] == 10_000_000
    assert rec.discarded[0].name == "no_concurrency_limit"


@pytest.mark.parametrize(
    "compose_detected,horizontal_scaling,expected_safe",
    [
        (False, False, True),
        (False, True, True),
        (True, True, True),
        (True, False, False),
    ],
)
def test_is_spot_safe_truth_table(
    compose_detected: bool, horizontal_scaling: bool, expected_safe: bool
) -> None:
    assert _is_spot_safe(compose_detected, horizontal_scaling) is expected_safe


def test_spot_forbidden_when_only_one_replica_at_lowest_traffic() -> None:
    """Single replica at the lowest traffic level, despite sizing that
    "scales" between 1K-100K users. compose_detected=False proves this is a
    genuinely new protection, not the mission's compose+no-scaling rule."""
    raw = json.loads((FIXTURES_DIR / "sample_input.json").read_text(encoding="utf-8"))
    raw["stack_detection"]["container"]["compose_detected"] = False
    analysis = RepoAnalysisInput.model_validate(raw)
    decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "4096", "task_memory": "8192"},
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )

    rec = optimize_finops(analysis, decision)

    assert rec.context["horizontal_scaling_detected"] is True
    assert rec.context["has_redundancy_at_low_traffic"] is False
    assert rec.recommended.name != "spot"
    spot_discarded = next(d for d in rec.discarded if d.name == "spot")
    assert "1K" in spot_discarded.reason or "faible" in spot_discarded.reason


def test_ecs_forbids_spot_when_compose_and_no_scaling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The mission's hard rule, forced deterministically: compose_detected
    stays true (from the fixture) but scaling is monkeypatched to false."""
    monkeypatch.setattr(finops_optimizer, "_detects_horizontal_scaling", lambda decision: False)
    analysis = _load_analysis("sample_input.json")
    assert analysis.stack_detection.container.compose_detected is True
    decision = decide_architecture(analysis)

    rec = optimize_finops(analysis, decision)

    assert rec.recommended.name != "spot"
    spot_discarded = next(d for d in rec.discarded if d.name == "spot")
    assert "compose_detected" in spot_discarded.reason
    # ECS's baseline already assumes Graviton pricing -> falls through to reserved_3yr
    assert rec.recommended.name == "reserved_3yr"


def test_ec2_forbids_spot_recommends_graviton_instead_of_reserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EC2's baseline is NOT Graviton-priced (module 2 only picks x86) — so
    when Spot is unsafe, Graviton is a real, additive saving and should win
    over jumping straight to Reserved."""
    monkeypatch.setattr(finops_optimizer, "_detects_horizontal_scaling", lambda decision: False)
    raw = json.loads((FIXTURES_DIR / "sample_input.json").read_text(encoding="utf-8"))
    raw["stack_detection"]["container"]["compose_detected"] = True
    analysis = RepoAnalysisInput.model_validate(raw)
    decision = DecisionResult(
        compute_type="ec2",
        sizing={"instance_type": "t3.medium"},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 1.0},
    )

    rec = optimize_finops(analysis, decision)

    assert rec.recommended.name == "graviton"
    assert rec.context["already_graviton_priced"] is False


def test_detects_horizontal_scaling_false_for_oversized_task() -> None:
    """A task so large that even 100K users fits in one replica -> no scaling
    detected; computed from module 5, not fixed."""
    decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "204800", "task_memory": "409600"},
        score_breakdown={"ecs": 1.0, "lambda": 0.0, "ec2": 0.0},
    )
    assert finops_optimizer._detects_horizontal_scaling(decision) is False


def test_unknown_compute_type_raises_key_error() -> None:
    """DecisionResult's own Literal type prevents this in practice, but the
    dispatch dict must fail loudly, not silently, if it ever happened."""
    analysis = _load_analysis("sample_input.json")
    decision = decide_architecture(analysis)
    decision.compute_type = "serverless-mystery"  # type: ignore[assignment]
    with pytest.raises(KeyError):
        optimize_finops(analysis, decision)


def test_recommendation_rejects_wrong_discarded_type() -> None:
    from pydantic import ValidationError

    from core.finops_optimizer import FinOpsRecommendation, OptimizationOption

    with pytest.raises(ValidationError):
        FinOpsRecommendation(
            recommended=OptimizationOption(name="spot", reason="ok"),
            discarded="not a list",  # type: ignore[arg-type]
            context={},
        )


def test_s3_static_site_optimization_is_noop() -> None:
    """Regression (live nitjsefni.eu smoke): routing a bare static site to S3
    crashed the pipeline at finops_optimizer with a KeyError ('s3') because the
    optimizer map had no S3 entry. Static hosting has no compute to optimize —
    the stage must return a no-op recommendation, not crash."""
    raw = _load_raw("sample_input.json")
    raw["stack_detection"]["container"] = None
    raw["stack_detection"]["containers"] = []
    raw["stack_detection"]["database"] = None
    raw["stack_detection"]["frameworks"] = []
    raw["stack_detection"]["primary_language"] = "html"
    analysis = RepoAnalysisInput.model_validate(raw)
    decision = DecisionResult(
        compute_type="s3",
        sizing={},
        score_breakdown={"ecs": 0.0, "lambda": 0.0, "ec2": 0.0, "s3": 10.0},
    )
    result = optimize_finops(analysis, decision)
    assert result.recommended.name == "no_compute_to_optimize"
    assert result.context == {"compute_type": "s3"}
