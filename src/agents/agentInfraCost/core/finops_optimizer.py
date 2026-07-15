"""Module 6: proposes a deterministic FinOps optimization for the decided
compute type: Spot, Reserved, Graviton, or On-Demand.

Rules are evaluated purely over generic stack signals (container topology,
database presence, codebase maturity) — never over a specific framework
name, and this module never generates free-form text itself. Turning its
`recommended_option` / `discarded_options` / `context_used` into a
human-readable justification is llm_enrichment's job (module 10); this
module only produces the deterministic decision and short, templated
reasons that llm_enrichment can safely quote or paraphrase.
"""

from __future__ import annotations

import logging

from ..models.input_models import InfraCostAgentInput
from ..models.internal_models import DecisionResult, FinOpsOption, FinOpsResult
from .decision_engine import LARGE_CODEBASE_LOC_THRESHOLD

logger = logging.getLogger(__name__)

_ALL_OPTIONS: tuple[str, ...] = ("spot", "reserved", "graviton", "on_demand")

# Options with no meaningful equivalent in AWS Lambda's per-invocation billing model.
_NOT_APPLICABLE_TO_LAMBDA: tuple[str, ...] = ("spot", "reserved")


def _extract_signals(payload: InfraCostAgentInput) -> dict[str, bool]:
    """Reduces the validated input to the generic signals this module rules on.

    `compose_detected` is used as a proxy for "coordinated, stateful
    production topology" and `database_present` as a proxy for "workload
    that must stay up" — neither depends on a specific framework or
    database engine name.
    """
    stack = payload.stack_detection
    compose_detected = bool(stack.container.compose_detected)
    database_present = stack.database is not None
    return {
        "critical_stateful": compose_detected,
        "database_present": database_present,
        "interruption_tolerant": not compose_detected and not database_present,
        "has_usage_history_proxy": payload.repo_metadata.loc >= LARGE_CODEBASE_LOC_THRESHOLD,
    }


def _recommend_for_ecs_or_ec2(signals: dict[str, bool]) -> tuple[str, str]:
    """Returns (recommended_option, reason) for ECS/EC2, where Spot,
    Reserved and Graviton are all meaningful, mutually-exclusive picks."""
    if signals["interruption_tolerant"]:
        return "spot", (
            "No Docker Compose topology and no database were detected, suggesting an "
            "interruption-tolerant workload; Spot offers the largest savings with "
            "acceptable risk here."
        )
    if signals["has_usage_history_proxy"]:
        return "reserved", (
            "A stateful/critical topology was detected on a codebase large enough to "
            "suggest an established, steady-state production workload, which is what "
            "Reserved pricing is designed to discount."
        )
    return "graviton", (
        "A stateful/critical topology was detected, but the codebase is not yet large "
        "enough to justify committing to Reserved capacity; Graviton gives a "
        "no-commitment, no-interruption-risk saving instead."
    )


def _discard_reason(option: str, signals: dict[str, bool], recommended: str) -> str:
    """Short, templated reason for why `option` was not the pick — never
    free-form text, so llm_enrichment can quote it verbatim if it wants to."""
    if option == "spot":
        return (
            "Spot could be interrupted at any time; the detected stateful/critical "
            "topology (Docker Compose and/or a database) makes that too risky here."
        )
    if option == "reserved":
        if recommended == "spot":
            return (
                "Reserved requires a 1-3 year commitment that isn't justified for a "
                "workload assessed as interruption-tolerant."
            )
        return "The codebase isn't yet large enough to justify a Reserved capacity commitment."
    if option == "graviton":
        return (
            f"Graviton could still be layered on top of the recommended '{recommended}' "
            "option for additional savings, but this module reports one primary pick."
        )
    # option == "on_demand"
    return "On-Demand is the undiscounted baseline, superseded by the recommended option above."


def optimize(payload: InfraCostAgentInput, decision: DecisionResult) -> FinOpsResult:
    """Selects a single recommended cost optimization for `decision.compute_type`.

    :raises ValueError: if decision.compute_type is not one of ecs/lambda/ec2
    """
    signals = _extract_signals(payload)

    if decision.compute_type in ("ecs", "ec2"):
        recommended, _reason = _recommend_for_ecs_or_ec2(signals)
        discarded = [
            FinOpsOption(
                name=option,
                recommended=False,
                reason=_discard_reason(option, signals, recommended),
            )
            for option in _ALL_OPTIONS
            if option != recommended
        ]
    elif decision.compute_type == "lambda":
        recommended = "graviton"
        discarded = []
        for option in _ALL_OPTIONS:
            if option == recommended:
                continue
            if option in _NOT_APPLICABLE_TO_LAMBDA:
                reason = "Not applicable to AWS Lambda's per-invocation billing model."
            else:
                reason = "Graviton already captures this workload's available savings with no tradeoff."
            discarded.append(FinOpsOption(name=option, recommended=False, reason=reason))
    else:
        raise ValueError(f"Unknown compute_type: {decision.compute_type!r}")

    logger.info(
        "finops_optimizer recommended '%s' for job_id=%s (compute_type=%s)",
        recommended,
        payload.job_id,
        decision.compute_type,
    )

    return FinOpsResult(
        recommended_option=recommended,
        discarded_options=discarded,
        context_used={**signals, "compute_type": decision.compute_type},
    )
