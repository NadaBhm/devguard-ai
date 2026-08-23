"""Step 10 (optional): explanatory text via OpenRouter.

Turns already-frozen results (decision, cost, FinOps) into human-readable text for
the ``enrichment`` block — NEVER influences a decision or a number. Unset key,
failed call or timeout falls back to static deterministic templates;
enrichment_source is "llm" only if all three texts came from real calls.
"""

from __future__ import annotations

import logging
from typing import Final

from core.decision_engine import DecisionResult
from core.finops_optimizer import FinOpsRecommendation
from core.llm_provider import call_llm
from models.output_schema import Enrichment, EnrichmentSource, Money

logger = logging.getLogger(__name__)

_LLM_TIMEOUT_SECONDS: Final[float] = 20.0


def _call_llm(prompt: str, system_instruction: str) -> str | None:
    """Generated text, or None on ANY failure — every failure mode maps to the same
    "use the fallback" signal, never an exception the caller must handle."""
    try:
        return call_llm(
            prompt=prompt,
            system_instruction=system_instruction,
            temperature=0.2,
            timeout=_LLM_TIMEOUT_SECONDS,
            max_tokens=1024,
        )
    except Exception:
        logger.warning("LLM call failed; falling back to static text", exc_info=True)
        return None


def explain_architecture_decision(decision: DecisionResult) -> tuple[str, EnrichmentSource]:
    """Explain, in prose, why ``decision.compute_type`` won — using only
    the already-computed ``score_breakdown`` (module 2), never re-deciding."""
    scores = decision.score_breakdown

    if decision.decision_source == "llm" and decision.llm_reasoning:
        # Score breakdown is informational here (didn't decide compute_type), so
        # the fallback text must not claim it did.
        fallback = f"Architecture recommandée : {decision.compute_type}. {decision.llm_reasoning}"
        prompt = (
            f"Reformule en 2-3 phrases, en français, cette explication déjà donnée par un agent "
            f"de décision pour le choix d'architecture '{decision.compute_type}' : "
            f"{decision.llm_reasoning} N'invente aucune information nouvelle."
        )
    else:
        fallback = (
            f"Architecture recommandée : {decision.compute_type}. "
            f"Scores calculés — ecs: {scores['ecs']:.1f}, lambda: {scores['lambda']:.1f}, "
            f"ec2: {scores['ec2']:.1f}. Le type retenu a obtenu le score le plus élevé."
        )
        prompt = (
            f"Explique en 2-3 phrases, en français, pourquoi l'architecture '{decision.compute_type}' "
            f"a été choisie, à partir de ces scores déjà calculés : {scores}. "
            "N'invente aucun chiffre, utilise seulement ceux fournis."
        )

    text = _call_llm(
        prompt=prompt,
        system_instruction=(
            "Tu écris une explication factuelle et concise pour un rapport DevOps. "
            "Tu ne prends aucune décision, tu expliques seulement une décision déjà prise."
        ),
    )
    return (text, "llm") if text else (fallback, "fallback")


def summarize_cost_estimation(decision: DecisionResult, cost: Money) -> tuple[str, EnrichmentSource]:
    fallback = (
        f"Coût mensuel estimé pour {decision.compute_type} : {cost.amount} {cost.currency} "
        f"(fourchette {cost.range_min}-{cost.range_max} {cost.currency}, incertitude ±20%)."
    )
    text = _call_llm(
        prompt=(
            f"Résume en 2-3 phrases, en français, cette estimation de coût mensuel déjà "
            f"calculée pour une architecture '{decision.compute_type}' : montant={cost.amount}, "
            f"fourchette=[{cost.range_min}, {cost.range_max}] {cost.currency}. "
            "N'invente aucun chiffre, utilise seulement ceux fournis."
        ),
        system_instruction=(
            "Tu résumes un coût déjà calculé pour un rapport DevOps, factuellement et brièvement."
        ),
    )
    return (text, "llm") if text else (fallback, "fallback")


def explain_finops_choice(finops: FinOpsRecommendation) -> tuple[str, EnrichmentSource]:
    discarded_names = ", ".join(option.name for option in finops.discarded)
    fallback = (
        f"Stratégie FinOps recommandée : {finops.recommended.name}. {finops.recommended.reason} "
        f"Options écartées : {discarded_names}."
    )
    text = _call_llm(
        prompt=(
            f"Explique en 2-3 phrases, en français, cette recommandation FinOps déjà décidée : "
            f"stratégie retenue='{finops.recommended.name}' ({finops.recommended.reason}), "
            f"options écartées={discarded_names}. N'invente aucune raison, utilise seulement celles fournies."
        ),
        system_instruction=(
            "Tu expliques une décision d'optimisation de coût déjà prise, pour un rapport DevOps."
        ),
    )
    return (text, "llm") if text else (fallback, "fallback")


def build_enrichment(
    decision: DecisionResult, cost: Money, finops: FinOpsRecommendation
) -> Enrichment:
    """Assemble the enrichment block; source is "llm" only if all three texts came
    from real calls — any single fallback marks the whole block "fallback"."""
    architecture_explanation, arch_source = explain_architecture_decision(decision)
    cost_summary, cost_source = summarize_cost_estimation(decision, cost)
    finops_justification, finops_source = explain_finops_choice(finops)

    all_from_llm = arch_source == cost_source == finops_source == "llm"
    return Enrichment(
        architecture_explanation=architecture_explanation,
        cost_summary=cost_summary,
        finops_justification=finops_justification,
        enrichment_source="llm" if all_from_llm else "fallback",
    )
