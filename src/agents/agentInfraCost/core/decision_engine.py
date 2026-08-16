"""Step 2 of the InfraCost pipeline: decide compute_type and its sizing.

Reads the validated ``RepoAnalysisInput`` produced by ``input_validator`` and
picks one of ``ecs`` / ``lambda`` / ``ec2`` using a weighted scoring system
over *generic* properties of the detected stack (container presence,
docker-compose presence, database presence, framework presence, project
size) — never a specific framework name. This is what lets
``sample_input.json`` (FastAPI) and ``sample_input_variant_node_ecs.json``
(Express) both resolve to ``ecs`` despite sharing no framework in common:
the rule only looks at what they structurally have in common (a detected
container plus a docker-compose file).
"""

from __future__ import annotations

import os
from typing import Final, Literal

from pydantic import BaseModel

from models.input_schema import RepoAnalysisInput

ComputeType = Literal["ecs", "lambda", "ec2", "s3"]
DecisionSource = Literal["deterministic", "llm"]

# Below this many lines of code, an un-containerized project is considered
# small enough to run as a single stateless function rather than needing a
# persistent server.
SMALL_PROJECT_LOC_THRESHOLD: Final[int] = 2_000

# A bare static site (plain HTML/CSS/JS, no server-side runtime, no database,
# no container) is the one shape S3 should always win for. Anything else gets
# a strong negative so it can never win on accident.
_STATIC_PRIMARY_LANGUAGES: Final[frozenset[str]] = frozenset(
    {"html", "css", "javascript", "typescript"}
)

# ECS Fargate task_cpu/task_memory must be one of AWS's valid paired
# combinations; the high tier lives in _ECS_SIZE_DEFAULT.
_ECS_SIZE_TIERS: Final[tuple[tuple[int, str, str], ...]] = (
    (5_000, "256", "512"),
    (15_000, "512", "1024"),
)
_ECS_SIZE_DEFAULT: Final[tuple[str, str]] = ("1024", "2048")

_LAMBDA_SIZE_TIERS: Final[tuple[tuple[int, int], ...]] = (
    (200, 128),
    (1_000, 256),
)
_LAMBDA_SIZE_DEFAULT: Final[int] = 512

_EC2_SIZE_TIERS: Final[tuple[tuple[int, str], ...]] = (
    (5_000, "t3.micro"),
    (20_000, "t3.small"),
)
_EC2_SIZE_DEFAULT: Final[str] = "t3.medium"


class DecisionResult(BaseModel):
    """The architecture decision handed to the rest of the pipeline.

    ``sizing`` intentionally stays a loosely-typed dict here — it holds
    whichever keys make sense for ``compute_type`` (e.g. ``task_cpu`` /
    ``task_memory`` for ecs, ``memory_mb`` for lambda, ``instance_type`` for
    ec2). Strict per-type typing is enforced later, in the final output
    contract built by ``output_builder`` (module 7) — this is an internal,
    intermediate result, not the contract itself.
    """

    compute_type: ComputeType
    sizing: dict[str, int | str]
    score_breakdown: dict[str, float]

    # Both default so every existing caller of decide_architecture() (and
    # every existing test constructing a DecisionResult directly) is
    # unaffected. Only core.llm_architecture_advisor sets these to record
    # that an LLM, not this module's scoring, picked compute_type.
    decision_source: DecisionSource = "deterministic"
    llm_reasoning: str | None = None


def _is_static_site(analysis: RepoAnalysisInput) -> bool:
    stack = analysis.stack_detection
    if stack.container.detected or stack.database is not None:
        return False
    if stack.frameworks:
        return False
    return stack.primary_language.strip().lower() in _STATIC_PRIMARY_LANGUAGES


def _score_stack(analysis: RepoAnalysisInput) -> dict[str, float]:
    """Score each compute type from generic stack properties.

    Every signal here is structural (container? compose? database? any
    framework at all? how big is the project?) — never a specific
    framework, database engine, or language name. That is what lets two
    stacks with nothing in common but their shape (e.g. FastAPI+Postgres vs
    Express+MySQL) score identically.
    """
    container = analysis.stack_detection.container
    scores = {"ecs": 0.0, "lambda": 0.0, "ec2": 0.0, "s3": 0.0}

    if container.detected:
        scores["ecs"] += 3.0
        scores["lambda"] -= 3.0
        scores["ec2"] += 1.0
    else:
        scores["ecs"] -= 3.0
        if analysis.repo_metadata.loc < SMALL_PROJECT_LOC_THRESHOLD:
            scores["lambda"] += 5.0
        else:
            scores["ec2"] += 5.0

    if container.compose_detected:
        scores["ecs"] += 2.0

    if analysis.stack_detection.database is not None:
        scores["ecs"] += 1.0
        scores["lambda"] -= 1.0
        scores["ec2"] += 1.0

    if analysis.stack_detection.frameworks:
        scores["ecs"] += 1.0
        scores["lambda"] -= 1.0

    if _is_static_site(analysis):
        # A bare static site is a strict S3 candidate: hosting it on a
        # managed container/VM is wasted money. The bonus must dominate
        # everything the scoring above could have accumulated for a small
        # un-containerized project (lambda +5), so +8 guarantees the win.
        scores["s3"] += 8.0
        scores["lambda"] -= 5.0
        scores["ec2"] -= 5.0
        scores["ecs"] -= 5.0
    else:
        # S3 never wins for anything that isn't a bare static site.
        scores["s3"] -= 20.0

    return scores


def _choose_compute_type(scores: dict[str, float]) -> ComputeType:
    """Pick the highest-scoring compute type.

    Ties resolve in ``scores`` insertion order (ecs, then lambda, then
    ec2, then s3) since ``_score_stack`` always builds the dict in that
    order and ``max`` keeps the first maximal item — a managed container
    service is the safer default when signals are genuinely inconclusive.

    DeployOps has a real deployment path for ecs, ec2 and s3, but not yet
    for lambda (deployops/agent.py _normalize_payload still raises for it).
    To keep the real pipeline from deterministically landing on a compute
    type DeployOps refuses, lambda is excluded from selection while
    DEVGUARD_FORCE_COMPUTE_ECS (default "1") is set. Unit tests set it to
    "0" so the full scoring — lambda included — stays exercised. Remove the
    lambda exclusion entirely once DeployOps grows a lambda path.
    """
    if os.getenv("DEVGUARD_FORCE_COMPUTE_ECS", "1").lower() == "1":
        scores = {k: v for k, v in scores.items() if k != "lambda"}
    return max(scores, key=lambda compute_type: scores[compute_type])  # type: ignore[return-value]


def _size_ecs(analysis: RepoAnalysisInput) -> dict[str, int | str]:
    loc = analysis.repo_metadata.loc
    for threshold, cpu, memory in _ECS_SIZE_TIERS:
        if loc < threshold:
            return {"task_cpu": cpu, "task_memory": memory}
    cpu, memory = _ECS_SIZE_DEFAULT
    return {"task_cpu": cpu, "task_memory": memory}


def _size_lambda(analysis: RepoAnalysisInput) -> dict[str, int | str]:
    loc = analysis.repo_metadata.loc
    for threshold, memory_mb in _LAMBDA_SIZE_TIERS:
        if loc < threshold:
            return {"memory_mb": memory_mb}
    return {"memory_mb": _LAMBDA_SIZE_DEFAULT}


def _size_ec2(analysis: RepoAnalysisInput) -> dict[str, int | str]:
    loc = analysis.repo_metadata.loc
    for threshold, instance_type in _EC2_SIZE_TIERS:
        if loc < threshold:
            return {"instance_type": instance_type}
    return {"instance_type": _EC2_SIZE_DEFAULT}


def _size_s3(analysis: RepoAnalysisInput) -> dict[str, int | str]:
    return {}


_SIZERS = {
    "ecs": _size_ecs,
    "lambda": _size_lambda,
    "ec2": _size_ec2,
    "s3": _size_s3,
}


def compute_sizing(compute_type: ComputeType, analysis: RepoAnalysisInput) -> dict[str, int | str]:
    """Run the deterministic sizing rules for an already-chosen compute_type.

    Public entry point for callers that pick compute_type some other way
    than decide_architecture()'s scoring (currently:
    core.llm_architecture_advisor) but still want the exact same tested
    sizing tiers — never a second, possibly-drifting implementation of
    "how big should this ECS task / Lambda / EC2 instance be".
    """
    return _SIZERS[compute_type](analysis)


def decide_architecture(analysis: RepoAnalysisInput) -> DecisionResult:
    scores = _score_stack(analysis)
    compute_type = _choose_compute_type(scores)
    sizing = _SIZERS[compute_type](analysis)
    return DecisionResult(
        compute_type=compute_type,
        sizing=sizing,
        score_breakdown=scores,
    )
