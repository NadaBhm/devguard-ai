"""Module 2: determines ECS vs Lambda vs EC2 via generic weighted scoring.

The compute-type decision is a weighted sum over structural properties of
the detected stack — container presence, Docker Compose presence, presence
of *any* web framework, database presence, codebase size — never a specific
framework or database name. This is what lets the same logic classify a
FastAPI+Postgres repo and an Express+MySQL repo identically (both ECS)
while classifying a small, containerless script as Lambda: the reasoning
never inspects *which* framework or database was detected, only whether
one was detected at all.

Sizing (task_cpu/task_memory, Lambda memory/timeout, EC2 instance type) is
computed from a small "complexity points" tally over the same kind of
generic signals, mapped onto standard AWS tiers — never copied from an
example.
"""

from __future__ import annotations

import logging

from ..models.common import ComputeType
from ..models.input_models import InfraCostAgentInput
from ..models.internal_models import (
    DecisionResult,
    Ec2Sizing,
    EcsSizing,
    LambdaSizing,
    ScoreBreakdown,
)

logger = logging.getLogger(__name__)

# --- platform-wide defaults: identical for every job, never derived from input ---

ECS_CLUSTER_NAME = "devguard-cluster"
EC2_KEY_PAIR_NAME = "devguard-keypair"
EC2_DEFAULT_AMI_ID = "ami-0000000000000000"  # placeholder pending real AMI lookup (v2)
DEFAULT_HEALTH_CHECK_PATH = "/health"
DEFAULT_HEALTH_CHECK_PORT = 8080
DEFAULT_TIMEOUT_MINUTES = 5
DEFAULT_MIN_HEALTHY_PERCENT = 50
DEFAULT_MAX_PERCENT = 200

LARGE_CODEBASE_LOC_THRESHOLD = 1000

_LAMBDA_RUNTIME_BY_LANGUAGE = {
    "python": "python3.12",
    "javascript": "nodejs20.x",
    "typescript": "nodejs20.x",
    "java": "java21",
    "go": "provided.al2023",
    "ruby": "ruby3.3",
}
_LAMBDA_HANDLER_BY_LANGUAGE = {
    "python": "main.handler",
    "javascript": "index.handler",
    "typescript": "index.handler",
    "java": "Main::handleRequest",
    "go": "bootstrap",
    "ruby": "main.handler",
}
_DEFAULT_LAMBDA_RUNTIME = "provided.al2023"
_DEFAULT_LAMBDA_HANDLER = "main.handler"

# Standard resource tiers, indexed by a 0-4 "complexity points" score.
_ECS_TASK_TIERS: list[tuple[int, int]] = [
    (256, 512),
    (256, 1024),
    (512, 1024),
    (512, 2048),
    (1024, 2048),
]
_LAMBDA_TIERS: list[tuple[int, int]] = [(128, 15), (256, 30), (512, 60), (1024, 120)]
_EC2_INSTANCE_TIERS: list[str] = ["t3.micro", "t3.small", "t3.medium", "t3.large"]

# --- scoring weights (tunable; encode relative importance of each generic signal) ---

_ECS_WEIGHTS = {
    "container_detected": 3.0,
    "compose_detected": 2.0,
    "has_framework": 1.5,
    "database_present": 1.0,
    "is_large_codebase": 0.5,
}
_LAMBDA_WEIGHTS = {
    "no_container": 3.0,
    "no_framework": 1.5,
    "is_small_codebase": 2.0,
    "no_database": 0.5,
}
_EC2_WEIGHTS = {
    "container_detected": 1.0,
    "container_without_compose": 1.0,
    "database_present": 1.5,
    "no_container_and_large": 2.0,
}

# On a tie, prefer the safest general-purpose option first.
_TIE_BREAK_PRIORITY: tuple[ComputeType, ...] = ("ecs", "ec2", "lambda")


def _extract_signals(payload: InfraCostAgentInput) -> dict[str, bool]:
    """Reduces the validated stack detection to generic boolean signals.

    No signal here is keyed on a specific framework or database name —
    only on whether the *category* of thing was detected at all.
    """
    stack = payload.stack_detection
    repo = payload.repo_metadata

    return {
        "container_detected": stack.container.detected,
        "compose_detected": bool(stack.container.compose_detected),
        "has_framework": len(stack.frameworks) > 0,
        "database_present": stack.database is not None,
        "is_large_codebase": repo.loc >= LARGE_CODEBASE_LOC_THRESHOLD,
    }


def _score_compute_types(signals: dict[str, bool]) -> ScoreBreakdown:
    container_detected = signals["container_detected"]
    compose_detected = signals["compose_detected"]
    has_framework = signals["has_framework"]
    database_present = signals["database_present"]
    is_large_codebase = signals["is_large_codebase"]

    ecs_score = (
        _ECS_WEIGHTS["container_detected"] * container_detected
        + _ECS_WEIGHTS["compose_detected"] * compose_detected
        + _ECS_WEIGHTS["has_framework"] * has_framework
        + _ECS_WEIGHTS["database_present"] * database_present
        + _ECS_WEIGHTS["is_large_codebase"] * is_large_codebase
    )
    lambda_score = (
        _LAMBDA_WEIGHTS["no_container"] * (not container_detected)
        + _LAMBDA_WEIGHTS["no_framework"] * (not has_framework)
        + _LAMBDA_WEIGHTS["is_small_codebase"] * (not is_large_codebase)
        + _LAMBDA_WEIGHTS["no_database"] * (not database_present)
    )
    ec2_score = (
        _EC2_WEIGHTS["container_detected"] * container_detected
        + _EC2_WEIGHTS["container_without_compose"]
        * (container_detected and not compose_detected)
        + _EC2_WEIGHTS["database_present"] * database_present
        + _EC2_WEIGHTS["no_container_and_large"]
        * ((not container_detected) and is_large_codebase)
    )

    return ScoreBreakdown(
        ecs_score=ecs_score,
        lambda_score=lambda_score,
        ec2_score=ec2_score,
        signals=signals,
    )


def _pick_compute_type(scores: ScoreBreakdown) -> ComputeType:
    by_type: dict[ComputeType, float] = {
        "ecs": scores.ecs_score,
        "lambda": scores.lambda_score,
        "ec2": scores.ec2_score,
    }
    best_score = max(by_type.values())
    for compute_type in _TIE_BREAK_PRIORITY:
        if by_type[compute_type] == best_score:
            return compute_type
    raise AssertionError("unreachable: _TIE_BREAK_PRIORITY must cover all compute types")


def _complexity_points(payload: InfraCostAgentInput, *, database_present: bool) -> int:
    """Generic 0-4 complexity tally used to pick a resource tier.

    Never reads a specific framework/database name — only counts (multiple
    frameworks present, database present, codebase size, file count).
    """
    stack = payload.stack_detection
    repo = payload.repo_metadata
    points = 0
    if database_present:
        points += 1
    if len(stack.frameworks) >= 2:
        points += 1
    if repo.loc >= 5000:
        points += 1
    if repo.total_files >= 100:
        points += 1
    return points


def _size_ecs(payload: InfraCostAgentInput, points: int) -> EcsSizing:
    tier_index = min(points, len(_ECS_TASK_TIERS) - 1)
    task_cpu, task_memory = _ECS_TASK_TIERS[tier_index]
    return EcsSizing(
        cluster=ECS_CLUSTER_NAME,
        service_name=f"{payload.repo_metadata.name}-service",
        task_cpu=task_cpu,
        task_memory=task_memory,
        health_check_path=DEFAULT_HEALTH_CHECK_PATH,
        health_check_port=DEFAULT_HEALTH_CHECK_PORT,
        timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
        min_healthy_percent=DEFAULT_MIN_HEALTHY_PERCENT,
        max_percent=DEFAULT_MAX_PERCENT,
    )


def _size_lambda(payload: InfraCostAgentInput, points: int) -> LambdaSizing:
    tier_index = min(points, len(_LAMBDA_TIERS) - 1)
    memory_mb, timeout_seconds = _LAMBDA_TIERS[tier_index]
    language = payload.stack_detection.primary_language.lower()
    return LambdaSizing(
        function_name=payload.repo_metadata.name,
        runtime=_LAMBDA_RUNTIME_BY_LANGUAGE.get(language, _DEFAULT_LAMBDA_RUNTIME),
        memory_mb=memory_mb,
        timeout_seconds=timeout_seconds,
        handler=_LAMBDA_HANDLER_BY_LANGUAGE.get(language, _DEFAULT_LAMBDA_HANDLER),
        reserved_concurrency=None,
    )


def _size_ec2(payload: InfraCostAgentInput, points: int) -> Ec2Sizing:
    tier_index = min(points, len(_EC2_INSTANCE_TIERS) - 1)
    return Ec2Sizing(
        instance_type=_EC2_INSTANCE_TIERS[tier_index],
        ami_id=EC2_DEFAULT_AMI_ID,
        instance_count=2 if points >= 3 else 1,
        key_pair_name=EC2_KEY_PAIR_NAME,
        health_check_path=DEFAULT_HEALTH_CHECK_PATH,
        health_check_port=DEFAULT_HEALTH_CHECK_PORT,
        timeout_minutes=DEFAULT_TIMEOUT_MINUTES,
    )


def _build_reasoning(
    signals: dict[str, bool], scores: ScoreBreakdown, compute_type: ComputeType
) -> list[str]:
    return [
        f"container_detected={signals['container_detected']}",
        f"compose_detected={signals['compose_detected']}",
        f"has_framework={signals['has_framework']}",
        f"database_present={signals['database_present']}",
        f"is_large_codebase={signals['is_large_codebase']}",
        f"scores: ecs={scores.ecs_score:.2f}, lambda={scores.lambda_score:.2f}, "
        f"ec2={scores.ec2_score:.2f}",
        f"selected compute_type='{compute_type}'",
    ]


def decide(payload: InfraCostAgentInput) -> DecisionResult:
    """Determines ECS vs Lambda vs EC2 for `payload` and computes its sizing.

    Assumes `payload` already passed input_validator's checks (status
    "completed", stack_detection present, confidence >= 0.5); this function
    does not re-validate those preconditions.
    """
    signals = _extract_signals(payload)
    scores = _score_compute_types(signals)
    compute_type = _pick_compute_type(scores)
    points = _complexity_points(payload, database_present=signals["database_present"])

    ecs_sizing: EcsSizing | None = None
    lambda_sizing: LambdaSizing | None = None
    ec2_sizing: Ec2Sizing | None = None
    if compute_type == "ecs":
        ecs_sizing = _size_ecs(payload, points)
    elif compute_type == "lambda":
        lambda_sizing = _size_lambda(payload, points)
    else:
        ec2_sizing = _size_ec2(payload, points)

    reasoning = _build_reasoning(signals, scores, compute_type)
    logger.info(
        "decision_engine selected compute_type='%s' for job_id=%s",
        compute_type,
        payload.job_id,
    )

    return DecisionResult(
        compute_type=compute_type,
        ecs=ecs_sizing,
        lambda_=lambda_sizing,
        ec2=ec2_sizing,
        score_breakdown=scores,
        reasoning=reasoning,
    )
