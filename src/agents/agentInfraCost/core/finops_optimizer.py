"""Step 6 of the InfraCost pipeline: propose a FinOps optimization.

Proposes exactly one recommended cost-saving strategy per architecture —
Spot, Reserved (1yr/3yr) or Graviton for ECS/EC2, a ``reserved_concurrency``
cap for Lambda — always deterministically, from signals already computed by
earlier modules (never free-form text; that is module 10's job, using this
module's output as its factual basis).

The mission's hard rule: Spot must never be recommended for a service that
has a docker-compose setup (``compose_detected`` — often a sign of a
stateful, single-instance-minded local dev setup) AND shows no horizontal
scaling across load (module 5's scenarios never add a second replica) —
losing that one Spot replica to an AWS reclaim would mean a full outage,
not a capacity dip.

On top of that hard rule, Spot also requires a minimum redundancy margin:
even where scaling is technically detected (more replicas at 100K users
than at 1K), a service that still runs on a single replica at the *lowest*
modeled traffic level is fully exposed whenever traffic happens to be low.
Spot is only considered safe when at least ``_MIN_REPLICAS_FOR_SAFE_SPOT``
replicas are running even at the 1K-user scenario — losing one Spot replica
should never be able to drop capacity to zero, at any modeled load level.
"""

from __future__ import annotations

import math
from typing import Any, Final

from pydantic import BaseModel

from core.cost_estimator import _get_pricing, _load_pricing_data, _select_arch_family, estimate_cost
from core.decision_engine import DecisionResult
from core.scenario_simulator import simulate_load_scenarios
from models.input_schema import RepoAnalysisInput

# Average seconds in a month, used only to convert a monthly invocation
# volume into an approximate concurrent-execution figure for Lambda
# (Little's Law: concurrency ~= throughput x average duration).
_SECONDS_PER_MONTH: Final[int] = 30 * 24 * 3600
_LAMBDA_CONCURRENCY_SAFETY_MARGIN: Final[float] = 2.0
_LAMBDA_MIN_RESERVED_CONCURRENCY: Final[int] = 5

# A single Spot interruption must never be able to drop capacity to zero,
# even at the lightest modeled traffic level (1K users) — require at least
# this many replicas already running there before Spot is considered safe.
_MIN_REPLICAS_FOR_SAFE_SPOT: Final[int] = 2


class OptimizationOption(BaseModel):
    """One considered strategy — recommended or discarded, always with a reason."""

    name: str
    reason: str
    projected_monthly_savings: float | None = None


class FinOpsRecommendation(BaseModel):
    """The chosen strategy, what was ruled out and why, and the signals used."""

    recommended: OptimizationOption
    discarded: list[OptimizationOption]
    context: dict[str, Any]


def _detects_horizontal_scaling(decision: DecisionResult) -> bool:
    """True if replica/instance count actually grows between the 1K and
    100K user scenarios (module 5) — Lambda always scales this way by
    design, so it is trivially true there."""
    if decision.compute_type == "lambda":
        return True
    scenarios = simulate_load_scenarios(decision)
    count_key = "task_count" if decision.compute_type == "ecs" else "instance_count"
    counts = [scenario.sizing[count_key] for scenario in scenarios]
    return counts[-1] > counts[0]


def _is_spot_safe(compose_detected: bool, horizontal_scaling_detected: bool) -> bool:
    """The mission's one hard rule: never Spot for compose_detected +
    no horizontal scaling — everything else is safe."""
    return not (compose_detected and not horizontal_scaling_detected)


def _has_redundancy_at_low_traffic(decision: DecisionResult) -> bool:
    """At least ``_MIN_REPLICAS_FOR_SAFE_SPOT`` replicas already running at
    the lightest modeled scenario (1K users) — so even the quietest traffic
    period has enough redundancy to absorb one Spot interruption without
    dropping to zero capacity. Lambda has no replica concept, so it is
    trivially true there."""
    if decision.compute_type == "lambda":
        return True
    scenarios = simulate_load_scenarios(decision)
    count_key = "task_count" if decision.compute_type == "ecs" else "instance_count"
    lowest_traffic_count = scenarios[0].sizing[count_key]
    return lowest_traffic_count >= _MIN_REPLICAS_FOR_SAFE_SPOT


def _optimize_ecs_or_ec2(
    analysis: RepoAnalysisInput, decision: DecisionResult
) -> FinOpsRecommendation:
    pricing = _load_pricing_data()
    discounts = _get_pricing(pricing, "optimization_discounts")
    baseline = estimate_cost(decision)

    compose_detected = analysis.stack_detection.container.compose_detected
    horizontal_scaling = _detects_horizontal_scaling(decision)
    has_redundancy = _has_redundancy_at_low_traffic(decision)
    spot_safe = _is_spot_safe(compose_detected, horizontal_scaling) and has_redundancy
    already_graviton = _select_arch_family(decision) == "arm_graviton"

    def option(name: str, pct: float, reason: str) -> OptimizationOption:
        return OptimizationOption(
            name=name, reason=reason, projected_monthly_savings=round(baseline.amount * pct, 2)
        )

    if spot_safe:
        spot_reason = (
            "Le plus gros gain, sûr ici : le service scale horizontalement et garde au "
            f"moins {_MIN_REPLICAS_FOR_SAFE_SPOT} copies même au trafic le plus faible modélisé — "
            "perdre une instance/tâche Spot ne cause jamais une coupure totale."
        )
    elif compose_detected and not horizontal_scaling:
        spot_reason = (
            "Écarté : compose_detected=true et aucun scaling horizontal détecté — "
            "perdre l'unique instance/tâche Spot causerait une coupure complète."
        )
    else:
        spot_reason = (
            f"Écarté : moins de {_MIN_REPLICAS_FOR_SAFE_SPOT} copies au trafic le plus "
            "faible modélisé (1K utilisateurs) — une interruption Spot pourrait faire "
            "tomber la capacité à zéro précisément quand le trafic est faible."
        )
    spot = option("spot", discounts["spot_avg_pct"], spot_reason)
    graviton = option(
        "graviton",
        discounts["graviton_pct"],
        "Aucun engagement de durée, gain immédiat en changeant simplement l'architecture "
        "du processeur — le choix le plus sûr quand Spot est écarté."
        if not already_graviton
        else "Écarté : l'estimation de base (module 4) suppose déjà arm_graviton pour ce "
        "type de compute, ce n'est pas un gain supplémentaire.",
    )
    reserved_3yr = option(
        "reserved_3yr",
        discounts["reserved_3yr_pct"],
        "Le plus gros gain restant, au prix d'un engagement de 3 ans.",
    )
    reserved_1yr = option(
        "reserved_1yr",
        discounts["reserved_1yr_pct"],
        "Écarté : dominé par l'engagement 3 ans, qui économise davantage pour un "
        "engagement plus long mais du même ordre de contrainte.",
    )

    if spot_safe:
        recommended, discarded = spot, [graviton, reserved_3yr, reserved_1yr]
    elif decision.compute_type == "ec2" and not already_graviton:
        recommended, discarded = graviton, [spot, reserved_3yr, reserved_1yr]
    else:
        recommended, discarded = reserved_3yr, [spot, graviton, reserved_1yr]

    return FinOpsRecommendation(
        recommended=recommended,
        discarded=discarded,
        context={
            "compose_detected": compose_detected,
            "horizontal_scaling_detected": horizontal_scaling,
            "has_redundancy_at_low_traffic": has_redundancy,
            "already_graviton_priced": already_graviton,
        },
    )


def _optimize_lambda(decision: DecisionResult) -> FinOpsRecommendation:
    scenarios = simulate_load_scenarios(decision)
    peak_invocations = scenarios[-1].sizing["monthly_invocations"]
    avg_duration_seconds = 1.0  # matches cost_estimator's CostEstimationContext default

    peak_concurrency = (peak_invocations / _SECONDS_PER_MONTH) * avg_duration_seconds
    reserved_concurrency = max(
        _LAMBDA_MIN_RESERVED_CONCURRENCY,
        math.ceil(peak_concurrency * _LAMBDA_CONCURRENCY_SAFETY_MARGIN),
    )

    recommended = OptimizationOption(
        name="reserved_concurrency",
        reason=(
            f"Plafonne les exécutions simultanées à {reserved_concurrency} "
            f"(marge de {_LAMBDA_CONCURRENCY_SAFETY_MARGIN}x au-dessus du pic modélisé à "
            "100K utilisateurs) pour éviter une facturation incontrôlée en cas de pic de "
            "trafic imprévu au-delà des scénarios simulés."
        ),
    )
    discarded = [
        OptimizationOption(
            name="no_concurrency_limit",
            reason="Écarté : aucune protection contre un pic de trafic imprévu au-delà "
            "des scénarios modélisés (module 5).",
        )
    ]
    return FinOpsRecommendation(
        recommended=recommended,
        discarded=discarded,
        context={
            "peak_monthly_invocations": peak_invocations,
            "estimated_peak_concurrency": round(peak_concurrency, 2),
            "reserved_concurrency": reserved_concurrency,
        },
    )


_OPTIMIZERS = {
    "ecs": _optimize_ecs_or_ec2,
    "lambda": lambda analysis, decision: _optimize_lambda(decision),
    "ec2": _optimize_ecs_or_ec2,
}


def optimize_finops(analysis: RepoAnalysisInput, decision: DecisionResult) -> FinOpsRecommendation:
    """Propose one deterministic, justified cost-saving strategy.

    Args:
        analysis: Module 1's output — used for ``compose_detected``.
        decision: Module 2's output — the architecture decision.

    Returns:
        A ``FinOpsRecommendation`` naming the recommended strategy, the
        strategies ruled out and why, and the signals used to decide.
    """
    return _OPTIMIZERS[decision.compute_type](analysis, decision)
