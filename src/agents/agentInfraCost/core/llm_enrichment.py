"""Step 10 (optional) of the InfraCost pipeline: explanatory text via Gemini.

Turns results already computed by modules 2 (decision), 4 (cost) and 6
(FinOps) into human-readable text for the ``enrichment`` block. Strict
rules: this module NEVER influences a decision or a number — only text,
built from figures already frozen by the time it runs (last in the
pipeline). If ``GEMINI_API_KEY`` is unset, or the call fails or exceeds
``_GEMINI_TIMEOUT_SECONDS`` for any reason, it falls back to static,
deterministic Python text templates — never an error, never a block.
``enrichment_source`` reports "gemini" only if every one of the three texts
actually came from a real call; otherwise "fallback".
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Final

from core.decision_engine import DecisionResult
from core.finops_optimizer import FinOpsRecommendation
from models.output_schema import Enrichment, EnrichmentSource, Money

logger = logging.getLogger(__name__)

_GEMINI_TIMEOUT_SECONDS: Final[float] = 10.0

# The shared Gemini client lives at src/shared/, a sibling of src/agents/ —
# outside this package's own sys.path bootstrap (which only covers
# agentInfraCost/). Add src/ once so `shared.llm.gemini...` is importable
# regardless of the caller's working directory.
_SRC_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))


def _call_gemini(prompt: str, system_instruction: str) -> str | None:
    """Return generated text, or ``None`` if Gemini is unavailable or fails
    for any reason (missing key, network error, timeout, malformed
    response, ...) — every failure mode maps to the same "use the
    fallback" signal, never an exception the caller has to handle.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None

    try:
        from shared.llm.gemini.gemini_client import GeminiClient

        client = GeminiClient(api_key=api_key)

        async def _generate() -> str:
            response = await asyncio.wait_for(
                client.generate(prompt, system_instruction=system_instruction),
                timeout=_GEMINI_TIMEOUT_SECONDS,
            )
            return response.text

        return asyncio.run(_generate())
    except Exception:
        logger.warning("Gemini call failed; falling back to static text", exc_info=True)
        return None


def explain_architecture_decision(decision: DecisionResult) -> tuple[str, EnrichmentSource]:
    """Explain, in prose, why ``decision.compute_type`` won — using only
    the already-computed ``score_breakdown`` (module 2), never re-deciding."""
    scores = decision.score_breakdown

    if decision.decision_source == "llm" and decision.llm_reasoning:
        # The score breakdown here is informational only (kept for context,
        # see decide_architecture_via_llm's docstring) — it did NOT decide
        # compute_type here, so the fallback text must not claim it did.
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

    text = _call_gemini(
        prompt=prompt,
        system_instruction=(
            "Tu écris une explication factuelle et concise pour un rapport DevOps. "
            "Tu ne prends aucune décision, tu expliques seulement une décision déjà prise."
        ),
    )
    return (text, "gemini") if text else (fallback, "fallback")


def summarize_cost_estimation(decision: DecisionResult, cost: Money) -> tuple[str, EnrichmentSource]:
    fallback = (
        f"Coût mensuel estimé pour {decision.compute_type} : {cost.amount} {cost.currency} "
        f"(fourchette {cost.range_min}-{cost.range_max} {cost.currency}, incertitude ±20%)."
    )
    text = _call_gemini(
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
    return (text, "gemini") if text else (fallback, "fallback")


def explain_finops_choice(finops: FinOpsRecommendation) -> tuple[str, EnrichmentSource]:
    discarded_names = ", ".join(option.name for option in finops.discarded)
    fallback = (
        f"Stratégie FinOps recommandée : {finops.recommended.name}. {finops.recommended.reason} "
        f"Options écartées : {discarded_names}."
    )
    text = _call_gemini(
        prompt=(
            f"Explique en 2-3 phrases, en français, cette recommandation FinOps déjà décidée : "
            f"stratégie retenue='{finops.recommended.name}' ({finops.recommended.reason}), "
            f"options écartées={discarded_names}. N'invente aucune raison, utilise seulement celles fournies."
        ),
        system_instruction=(
            "Tu expliques une décision d'optimisation de coût déjà prise, pour un rapport DevOps."
        ),
    )
    return (text, "gemini") if text else (fallback, "fallback")


def build_enrichment(
    decision: DecisionResult, cost: Money, finops: FinOpsRecommendation
) -> Enrichment:
    """Assemble the full ``enrichment`` block from modules 2, 4 and 6.

    ``enrichment_source`` is ``"gemini"`` only if all three texts actually
    came from a real call — any single fallback marks the whole block
    ``"fallback"``, never overclaiming.
    """
    architecture_explanation, arch_source = explain_architecture_decision(decision)
    cost_summary, cost_source = summarize_cost_estimation(decision, cost)
    finops_justification, finops_source = explain_finops_choice(finops)

    all_from_gemini = arch_source == cost_source == finops_source == "gemini"
    return Enrichment(
        architecture_explanation=architecture_explanation,
        cost_summary=cost_summary,
        finops_justification=finops_justification,
        enrichment_source="gemini" if all_from_gemini else "fallback",
    )
