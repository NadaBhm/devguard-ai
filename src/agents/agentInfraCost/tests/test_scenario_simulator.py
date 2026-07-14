import math

import pytest

from agentInfraCost.core.scenario_simulator import (
    CONCURRENT_USERS_PER_EC2_INSTANCE,
    CONCURRENT_USERS_PER_ECS_TASK,
    REQUESTS_PER_ACTIVE_USER_PER_MONTH,
    SCENARIO_USER_LOADS,
    simulate_scenarios,
)
from agentInfraCost.models.internal_models import (
    DecisionResult,
    Ec2Sizing,
    EcsSizing,
    LambdaSizing,
    ScoreBreakdown,
)


def _score_breakdown() -> ScoreBreakdown:
    return ScoreBreakdown(ecs_score=0.0, lambda_score=0.0, ec2_score=0.0, signals={})


def _ecs_decision() -> DecisionResult:
    return DecisionResult(
        compute_type="ecs",
        ecs=EcsSizing(
            cluster="devguard-cluster",
            service_name="svc",
            task_cpu=512,
            task_memory=1024,
            health_check_path="/health",
            health_check_port=8080,
            timeout_minutes=5,
            min_healthy_percent=50,
            max_percent=200,
        ),
        score_breakdown=_score_breakdown(),
    )


def _lambda_decision() -> DecisionResult:
    return DecisionResult(
        compute_type="lambda",
        lambda_=LambdaSizing(
            function_name="fn",
            runtime="python3.12",
            memory_mb=256,
            timeout_seconds=30,
            handler="main.handler",
        ),
        score_breakdown=_score_breakdown(),
    )


def _ec2_decision() -> DecisionResult:
    return DecisionResult(
        compute_type="ec2",
        ec2=Ec2Sizing(
            instance_type="t3.small",
            ami_id="ami-0000000000000000",
            instance_count=1,
            key_pair_name="devguard-keypair",
            health_check_path="/status",
            health_check_port=80,
            timeout_minutes=10,
        ),
        score_breakdown=_score_breakdown(),
    )


class TestGeneralShape:
    def test_returns_one_result_per_scenario_user_load(self) -> None:
        results = simulate_scenarios(_ecs_decision())
        assert [r.user_load for r in results] == list(SCENARIO_USER_LOADS)

    def test_cost_increases_monotonically_with_user_load_for_every_type(self) -> None:
        for decision in (_ecs_decision(), _lambda_decision(), _ec2_decision()):
            results = simulate_scenarios(decision)
            amounts = [r.cost.amount for r in results]
            assert amounts == sorted(amounts)
            assert amounts[0] < amounts[-1]


class TestEcsScenarios:
    def test_task_count_scales_by_concurrency_ratio_not_linearly_on_cost_alone(self) -> None:
        results = simulate_scenarios(_ecs_decision())
        by_load = {r.user_load: r for r in results}
        assert by_load[1_000].compute_units == math.ceil(1_000 / CONCURRENT_USERS_PER_ECS_TASK)
        assert by_load[10_000].compute_units == math.ceil(10_000 / CONCURRENT_USERS_PER_ECS_TASK)
        assert by_load[100_000].compute_units == math.ceil(
            100_000 / CONCURRENT_USERS_PER_ECS_TASK
        )

    def test_boundary_low_1k_users(self) -> None:
        results = simulate_scenarios(_ecs_decision())
        result = next(r for r in results if r.user_load == 1_000)
        assert result.compute_units >= 1
        assert result.cost.amount > 0

    def test_boundary_high_100k_users(self) -> None:
        results = simulate_scenarios(_ecs_decision())
        result = next(r for r in results if r.user_load == 100_000)
        assert result.compute_units == math.ceil(100_000 / CONCURRENT_USERS_PER_ECS_TASK)

    def test_cost_scales_linearly_with_task_count(self) -> None:
        results = simulate_scenarios(_ecs_decision())
        by_load = {r.user_load: r for r in results}
        per_task_amount = by_load[1_000].cost.amount / by_load[1_000].compute_units
        for result in by_load.values():
            assert result.cost.amount == pytest.approx(
                per_task_amount * result.compute_units, abs=0.05
            )

    def test_graviton_reduces_cost(self) -> None:
        x86_results = simulate_scenarios(_ecs_decision(), use_graviton=False)
        graviton_results = simulate_scenarios(_ecs_decision(), use_graviton=True)
        for x86_result, graviton_result in zip(x86_results, graviton_results):
            assert graviton_result.cost.amount < x86_result.cost.amount


class TestLambdaScenarios:
    def test_compute_units_are_recalculated_monthly_invocations(self) -> None:
        results = simulate_scenarios(_lambda_decision())
        by_load = {r.user_load: r for r in results}
        assert (
            by_load[1_000].compute_units == 1_000 * REQUESTS_PER_ACTIVE_USER_PER_MONTH
        )
        assert (
            by_load[100_000].compute_units == 100_000 * REQUESTS_PER_ACTIVE_USER_PER_MONTH
        )

    def test_boundary_low_1k_users(self) -> None:
        results = simulate_scenarios(_lambda_decision())
        result = next(r for r in results if r.user_load == 1_000)
        assert result.cost.amount >= 0

    def test_boundary_high_100k_users_costs_much_more_than_low(self) -> None:
        results = simulate_scenarios(_lambda_decision())
        by_load = {r.user_load: r for r in results}
        assert by_load[100_000].cost.amount > by_load[1_000].cost.amount * 50


class TestEc2Scenarios:
    def test_instance_count_scales_by_concurrency_ratio(self) -> None:
        results = simulate_scenarios(_ec2_decision())
        by_load = {r.user_load: r for r in results}
        assert by_load[1_000].compute_units == math.ceil(
            1_000 / CONCURRENT_USERS_PER_EC2_INSTANCE
        )
        assert by_load[100_000].compute_units == math.ceil(
            100_000 / CONCURRENT_USERS_PER_EC2_INSTANCE
        )

    def test_boundary_low_1k_users(self) -> None:
        results = simulate_scenarios(_ec2_decision())
        result = next(r for r in results if r.user_load == 1_000)
        assert result.compute_units >= 1
        assert result.cost.amount > 0

    def test_boundary_high_100k_users(self) -> None:
        results = simulate_scenarios(_ec2_decision())
        result = next(r for r in results if r.user_load == 100_000)
        assert result.compute_units == math.ceil(100_000 / CONCURRENT_USERS_PER_EC2_INSTANCE)


class TestErrors:
    def test_raises_when_compute_type_has_no_matching_sizing_block(self) -> None:
        decision = DecisionResult(compute_type="ecs", ecs=None, score_breakdown=_score_breakdown())
        with pytest.raises(ValueError, match="decision.ecs is None"):
            simulate_scenarios(decision)
