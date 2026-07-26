import json

import pytest

from agentInfraCost.core.cost_estimator import (
    DEFAULT_LAMBDA_AVG_DURATION_SECONDS,
    DEFAULT_LAMBDA_MONTHLY_INVOCATIONS,
    estimate_cost,
    estimate_ec2_cost,
    estimate_ecs_cost,
    estimate_lambda_cost,
    load_pricing_table,
)
from agentInfraCost.models.exceptions import PricingDataError
from agentInfraCost.models.internal_models import (
    DecisionResult,
    Ec2Sizing,
    EcsSizing,
    LambdaSizing,
    ScoreBreakdown,
)


def _score_breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(ecs_score=0.0, lambda_score=0.0, ec2_score=0.0, signals={})


def _ecs_decision(task_cpu: int, task_memory: int) -> DecisionResult:
    return DecisionResult(
        compute_type="ecs",
        ecs=EcsSizing(
            cluster="devguard-cluster",
            service_name="svc",
            task_cpu=task_cpu,
            task_memory=task_memory,
            health_check_path="/health",
            health_check_port=8080,
            timeout_minutes=5,
            min_healthy_percent=50,
            max_percent=200,
        ),
        score_breakdown=_score_breakdown(),
    )


def _lambda_decision(memory_mb: int) -> DecisionResult:
    return DecisionResult(
        compute_type="lambda",
        lambda_=LambdaSizing(
            function_name="fn",
            runtime="python3.12",
            memory_mb=memory_mb,
            timeout_seconds=30,
            handler="main.handler",
        ),
        score_breakdown=_score_breakdown(),
    )


def _ec2_decision(instance_type: str, instance_count: int = 1) -> DecisionResult:
    return DecisionResult(
        compute_type="ec2",
        ec2=Ec2Sizing(
            instance_type=instance_type,
            ami_id="ami-0000000000000000",
            instance_count=instance_count,
            key_pair_name="devguard-keypair",
            health_check_path="/status",
            health_check_port=80,
            timeout_minutes=10,
        ),
        score_breakdown=_score_breakdown(),
    )


class TestPricingTableLoading:
    def test_loads_expected_top_level_keys(self) -> None:
        table = load_pricing_table()
        assert "ecs_fargate" in table
        assert "lambda" in table
        assert "ec2_on_demand_hourly" in table
        assert "ebs_gp3_per_gb_month" in table

    def test_table_is_read_from_disk_only_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        load_pricing_table.cache_clear()
        original_load = json.load
        call_count = {"n": 0}

        def counting_load(f):
            call_count["n"] += 1
            return original_load(f)

        monkeypatch.setattr(json, "load", counting_load)
        try:
            table_1 = load_pricing_table()
            table_2 = load_pricing_table()
            assert table_1 is table_2
            assert call_count["n"] == 1
        finally:
            load_pricing_table.cache_clear()


class TestEcsCost:
    def test_nominal_matches_hand_calculated_formula(self) -> None:
        table = load_pricing_table()
        result = estimate_ecs_cost(vcpu=1.0, memory_gb=2.0)
        expected = (
            table["ecs_fargate"]["x86"]["vcpu_per_hour"] * 1.0
            + table["ecs_fargate"]["x86"]["memory_gb_per_hour"] * 2.0
        ) * table["ecs_fargate"]["hours_per_month"]
        # amount is intentionally rounded to cents, so compare with a cent
        # of absolute tolerance rather than a tight relative one.
        assert result.amount == pytest.approx(expected, abs=0.01)
        assert result.range_min == pytest.approx(expected * 0.8, abs=0.01)
        assert result.range_max == pytest.approx(expected * 1.2, abs=0.01)

    def test_boundary_low_smallest_tier(self) -> None:
        """Smallest Fargate tier, standing in for a lightly-loaded ("~1K
        users") service."""
        result = estimate_ecs_cost(vcpu=0.25, memory_gb=0.5)
        assert result.amount > 0

    def test_boundary_high_large_tier_costs_more_than_low(self) -> None:
        """Large Fargate tier, standing in for a heavily-loaded ("~100K
        users") service — cost must scale up, not stay flat or invert."""
        low = estimate_ecs_cost(vcpu=0.25, memory_gb=0.5)
        high = estimate_ecs_cost(vcpu=4.0, memory_gb=8.0)
        assert high.amount > low.amount * 10

    def test_graviton_is_cheaper_than_x86_for_same_size(self) -> None:
        x86 = estimate_ecs_cost(vcpu=1.0, memory_gb=2.0, use_graviton=False)
        graviton = estimate_ecs_cost(vcpu=1.0, memory_gb=2.0, use_graviton=True)
        assert graviton.amount < x86.amount


class TestLambdaCost:
    def test_nominal_matches_hand_calculated_formula(self) -> None:
        table = load_pricing_table()
        result = estimate_lambda_cost(
            memory_mb=256, avg_duration_seconds=0.2, monthly_invocations=100_000
        )
        gb_second = table["lambda"]["x86"]["gb_second"]
        requests_per_million = table["lambda"]["x86"]["requests_per_million"]
        expected = (gb_second * 0.25 * 0.2 * 100_000) + (
            requests_per_million * 100_000 / 1_000_000
        )
        assert result.amount == pytest.approx(expected, abs=0.01)

    def test_boundary_low_1k_invocations(self) -> None:
        """At this low a volume, Lambda is genuinely near-free — the true
        cost (~$0.0006) rounds to $0.00, which is the correct, realistic
        output, not a bug. Only non-negativity is asserted here; the
        boundary_high test below covers meaningful scaling."""
        result = estimate_lambda_cost(
            memory_mb=128, avg_duration_seconds=0.2, monthly_invocations=1_000
        )
        assert result.amount >= 0

    def test_boundary_high_100k_invocations_costs_more_than_low(self) -> None:
        low = estimate_lambda_cost(
            memory_mb=128, avg_duration_seconds=0.2, monthly_invocations=1_000
        )
        high = estimate_lambda_cost(
            memory_mb=128, avg_duration_seconds=0.2, monthly_invocations=100_000
        )
        assert high.amount > low.amount * 50

    def test_graviton_is_cheaper_than_x86_for_same_usage(self) -> None:
        x86 = estimate_lambda_cost(
            memory_mb=256,
            avg_duration_seconds=0.2,
            monthly_invocations=100_000,
            use_graviton=False,
        )
        graviton = estimate_lambda_cost(
            memory_mb=256,
            avg_duration_seconds=0.2,
            monthly_invocations=100_000,
            use_graviton=True,
        )
        assert graviton.amount < x86.amount


class TestEc2Cost:
    def test_nominal_matches_hand_calculated_formula(self) -> None:
        table = load_pricing_table()
        result = estimate_ec2_cost(instance_type="t3.small", instance_count=1)
        expected = table["ec2_on_demand_hourly"]["t3.small"] * table["ecs_fargate"]["hours_per_month"]
        assert result.amount == pytest.approx(expected, abs=0.01)

    def test_boundary_low_single_smallest_instance(self) -> None:
        """Single t3.nano, standing in for a minimal ("~1K users") deployment."""
        result = estimate_ec2_cost(instance_type="t3.nano", instance_count=1)
        assert result.amount > 0

    def test_boundary_high_large_fleet_costs_more_than_low(self) -> None:
        """A 20-instance m5.large fleet, standing in for a heavily-loaded
        ("~100K users") deployment — must scale with both size and count."""
        low = estimate_ec2_cost(instance_type="t3.nano", instance_count=1)
        high = estimate_ec2_cost(instance_type="m5.large", instance_count=20)
        assert high.amount > low.amount * 100

    def test_unknown_instance_type_raises_pricing_data_error_naming_the_key(self) -> None:
        with pytest.raises(PricingDataError) as exc_info:
            estimate_ec2_cost(instance_type="z9.doesnotexist", instance_count=1)
        assert "z9.doesnotexist" in str(exc_info.value)

    def test_ebs_volume_adds_to_the_cost(self) -> None:
        without_ebs = estimate_ec2_cost(instance_type="t3.small", instance_count=1)
        with_ebs = estimate_ec2_cost(instance_type="t3.small", instance_count=1, ebs_gb=100.0)
        table = load_pricing_table()
        expected_ebs_addition = table["ebs_gp3_per_gb_month"] * 100.0
        assert with_ebs.amount - without_ebs.amount == pytest.approx(
            expected_ebs_addition, rel=1e-6
        )

    def test_ebs_scales_with_instance_count(self) -> None:
        one_instance = estimate_ec2_cost(instance_type="t3.small", instance_count=1, ebs_gb=50.0)
        two_instances = estimate_ec2_cost(
            instance_type="t3.small", instance_count=2, ebs_gb=50.0
        )
        # Doubling both compute and attached storage should roughly double cost.
        assert two_instances.amount == pytest.approx(one_instance.amount * 2, abs=0.01)


class TestRangeInvariant:
    @pytest.mark.parametrize(
        "result",
        [
            estimate_ecs_cost(vcpu=0.5, memory_gb=1.0),
            estimate_lambda_cost(
                memory_mb=256, avg_duration_seconds=0.2, monthly_invocations=50_000
            ),
            estimate_ec2_cost(instance_type="t3.medium", instance_count=3),
        ],
    )
    def test_range_is_always_plus_minus_20_percent_of_amount(self, result) -> None:
        assert result.range_min == pytest.approx(result.amount * 0.8, rel=1e-3)
        assert result.range_max == pytest.approx(result.amount * 1.2, rel=1e-3)
        assert result.range_min < result.amount < result.range_max


class TestEstimateCostDispatcher:
    def test_routes_ecs_decision_using_task_units_converted_to_vcpu_and_gb(self) -> None:
        decision = _ecs_decision(task_cpu=512, task_memory=1024)
        result = estimate_cost(decision)
        expected = estimate_ecs_cost(vcpu=0.5, memory_gb=1.0)
        assert result == expected

    def test_routes_lambda_decision_with_default_baseline_usage(self) -> None:
        decision = _lambda_decision(memory_mb=256)
        result = estimate_cost(decision)
        expected = estimate_lambda_cost(
            memory_mb=256,
            avg_duration_seconds=DEFAULT_LAMBDA_AVG_DURATION_SECONDS,
            monthly_invocations=DEFAULT_LAMBDA_MONTHLY_INVOCATIONS,
        )
        assert result == expected

    def test_routes_lambda_decision_with_overridden_scenario_usage(self) -> None:
        """Demonstrates how scenario_simulator (module 5) will reuse this
        dispatcher with per-load-tier usage figures instead of the default
        baseline."""
        decision = _lambda_decision(memory_mb=256)
        result = estimate_cost(
            decision,
            lambda_avg_duration_seconds=0.5,
            lambda_monthly_invocations=10_000,
        )
        expected = estimate_lambda_cost(
            memory_mb=256, avg_duration_seconds=0.5, monthly_invocations=10_000
        )
        assert result == expected

    def test_routes_ec2_decision(self) -> None:
        decision = _ec2_decision(instance_type="t3.medium", instance_count=2)
        result = estimate_cost(decision)
        expected = estimate_ec2_cost(instance_type="t3.medium", instance_count=2)
        assert result == expected

    def test_raises_when_compute_type_has_no_matching_sizing_block(self) -> None:
        decision = DecisionResult(
            compute_type="ecs", ecs=None, score_breakdown=_score_breakdown()
        )
        with pytest.raises(ValueError, match="decision.ecs is None"):
            estimate_cost(decision)
