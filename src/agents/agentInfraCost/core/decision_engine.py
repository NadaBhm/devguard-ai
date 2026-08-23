"""Step 2: decide compute_type and its sizing.

Weighted scoring over *generic* stack properties (container/compose/database/
framework presence, size) picks ecs/lambda/ec2 — never a specific framework name —
so structurally identical stacks (FastAPI vs Express) score identically; bare
static sites always go to S3.
"""

from __future__ import annotations

import os
from typing import Final, Literal

from pydantic import BaseModel

from models.input_schema import RepoAnalysisInput

ComputeType = Literal["ecs", "lambda", "ec2", "s3"]
DecisionSource = Literal["deterministic", "llm"]

# Below this many LOC, an un-containerized project is small enough to run as a
# single stateless function rather than needing a persistent server.
SMALL_PROJECT_LOC_THRESHOLD: Final[int] = 2_000

# A bare static site (no server runtime, database, container) is the one shape S3
# should always win; anything else gets a strong negative so it can't win by accident.
_STATIC_PRIMARY_LANGUAGES: Final[frozenset[str]] = frozenset(
    {"html", "css", "javascript", "typescript"}
)

# ECS Fargate sizing tiers: task_cpu/task_memory must be valid AWS paired combos
# (arbitrary values rejected at RegisterTaskDefinition). High tier: _ECS_SIZE_DEFAULT.
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
    """The architecture decision handed to the rest of the pipeline. ``sizing``
    stays loosely-typed (keys depend on compute_type); strict per-type typing is
    enforced later in output_builder's contract — this is intermediate, not contract.
    """

    compute_type: ComputeType
    sizing: dict[str, int | str]
    score_breakdown: dict[str, float]

    # Both defaulted so existing callers (and tests constructing DecisionResult
    # directly) are unaffected; only core.llm_architecture_advisor sets these.
    decision_source: DecisionSource = "deterministic"
    llm_reasoning: str | None = None


def _is_static_site(analysis: RepoAnalysisInput) -> bool:
    stack = analysis.stack_detection
    if (stack.container is not None and stack.container.detected) or stack.database is not None:
        return False
    if stack.frameworks:
        return False
    # npm/yarn/pnpm usually means a Node server — unless there's no server
    # entry file, in which case it's dev tooling on a pure static site
    # (e.g. startbootstrap templates).
    if stack.build_tool in ("npm", "yarn", "pnpm"):
        entry = {"server.js", "app.js", "index.js", "main.js"}
        if not any(f.lower() in entry for f in stack.detected_files):
            return True
        return False
    return stack.primary_language.strip().lower() in _STATIC_PRIMARY_LANGUAGES


def _score_stack(analysis: RepoAnalysisInput) -> dict[str, float]:
    """Score each compute type from generic structural properties — container?
    compose? database? any framework? how big? — never a specific framework/engine/
    language, so differently-named but similarly-shaped stacks score identically.
    """
    container = analysis.stack_detection.container
    scores = {"ecs": 0.0, "lambda": 0.0, "ec2": 0.0, "s3": 0.0}

    # Container is Optional in the schema (a bare static site has none); treat
    # absent as "not detected" rather than crashing the scoring.
    container_detected = container is not None and container.detected

    if container_detected:
        scores["ecs"] += 3.0
        scores["lambda"] -= 3.0
        scores["ec2"] += 1.0
    else:
        scores["ecs"] -= 3.0
        if analysis.repo_metadata.loc < SMALL_PROJECT_LOC_THRESHOLD:
            scores["lambda"] += 5.0
        else:
            scores["ec2"] += 5.0

    if container is not None and container.compose_detected:
        scores["ecs"] += 2.0

    # sqlite is a local file, not a server — no managed DB to size for.
    if analysis.stack_detection.database is not None and analysis.stack_detection.database != "sqlite":
        scores["ecs"] += 1.0
        scores["lambda"] -= 1.0
        scores["ec2"] += 1.0

    if analysis.stack_detection.frameworks:
        scores["ecs"] += 1.0
        scores["lambda"] -= 1.0

    if _is_static_site(analysis):
        # +8 dominates everything the scoring above accumulates (lambda max +5) —
        # hosting a static site on managed compute is wasted money.
        scores["s3"] += 8.0
        scores["lambda"] -= 5.0
        scores["ec2"] -= 5.0
        scores["ecs"] -= 5.0
    else:
        # S3 never wins for anything that isn't a bare static site.
        scores["s3"] -= 20.0

    return scores


def _choose_compute_type(scores: dict[str, float]) -> ComputeType:
    """Pick the highest-scoring type; ties resolve in insertion order (ecs, lambda,
    ec2, s3 — managed containers are the safer default). While DEVGUARD_FORCE_COMPUTE_ECS
    (default "1") lambda is excluded: DeployOps has no lambda deploy path yet
    (deployops/agent.py raises), so the real pipeline must not land on it; tests set
    "0" to exercise full scoring. Remove once DeployOps grows a lambda path."""
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


# JS frameworks that compile/bundle at runtime keep a watch/compiler resident, OOMing
# a 512MB Fargate task (devverse: verified live) — bump to the next valid tier below.
_HEAVY_FRONTEND_FRAMEWORKS: Final[frozenset[str]] = frozenset({
    "next", "nextjs", "react", "vue", "nuxt", "angular", "svelte", "astro",
})
_HEAVY_FRONTEND_TOOLS: Final[frozenset[str]] = frozenset({"npm", "yarn", "pnpm", "bun"})

# Valid Fargate CPU/memory pairings, smallest -> largest: bumping memory alone yields
# an invalid combo ("memory is too large for the CPU"), so move up the whole pair.
_FARGATE_TIERS: Final[tuple[tuple[str, str], ...]] = (
    ("256", "512"),
    ("512", "1024"),
    ("1024", "2048"),
    ("2048", "4096"),
    ("4096", "8192"),
)


def _is_heavy_frontend(analysis: RepoAnalysisInput) -> bool:
    stack = analysis.stack_detection
    container = stack.container
    if (container is not None and container.detected) or stack.database is not None:
        return False
    has_framework = any(
        fw.lower() in _HEAVY_FRONTEND_FRAMEWORKS for fw in stack.frameworks
    )
    has_tool = stack.build_tool and stack.build_tool.lower() in _HEAVY_FRONTEND_TOOLS
    return has_framework and has_tool


def _size_ecs_memory_bumped(analysis: RepoAnalysisInput) -> dict[str, int | str]:
    """Size an ECS task, raising memory for heavy JS frontends. Bare static sites
    are already S3, so the frontends landing here all run servers (Next SSR, Nuxt...)
    — exactly the ones whose build/dev server OOMs at 512MB."""
    sizing = _size_ecs(analysis)
    if not _is_heavy_frontend(analysis):
        return sizing
    cpu, memory = str(sizing["task_cpu"]), str(sizing["task_memory"])
    for i, (c, m) in enumerate(_FARGATE_TIERS):
        if c == cpu and m == memory and i + 1 < len(_FARGATE_TIERS):
            return {"task_cpu": _FARGATE_TIERS[i + 1][0], "task_memory": _FARGATE_TIERS[i + 1][1]}
    return sizing


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
    "ecs": _size_ecs_memory_bumped,
    "lambda": _size_lambda,
    "ec2": _size_ec2,
    "s3": _size_s3,
}


def compute_sizing(compute_type: ComputeType, analysis: RepoAnalysisInput) -> dict[str, int | str]:
    """Run the deterministic sizing rules for an already-chosen compute_type —
    public so callers picking compute_type another way (llm_architecture_advisor)
    reuse the same tested tiers instead of a drifting second implementation."""
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
