"""Module 10 (optional): turns already-computed results into natural-language
explanations, via Gemini when available, via static templates otherwise.

Strict rules enforced here:
  - Never influences a decision, a number, or Terraform content — only adds
    explanatory text to the output's "enrichment" block.
  - If GEMINI_API_KEY is absent, or the `google-generativeai` package itself
    isn't installed, or the call fails/times out/returns empty for any
    reason: falls back to a static template built from the same already-
    computed variables. Never raises, never blocks the pipeline.
  - Every Gemini call has a hard timeout (DEFAULT_TIMEOUT_SECONDS).
  - Called last, after decision_engine / cost_estimator / finops_optimizer
    have already produced their (deterministic) results.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

from ..models.internal_models import CostEstimateResult, DecisionResult, FinOpsResult
from ..models.output_models import Enrichment

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 10.0

EnrichmentSource = Literal["gemini", "fallback"]


# --- prompts (only used when a Gemini call is actually attempted) ----------


def _architecture_prompt(decision: DecisionResult) -> str:
    return (
        "In 2-3 plain-English sentences for a non-technical reader, explain why "
        f"'{decision.compute_type}' was chosen as the AWS compute target. "
        f"Scoring signals used: {decision.score_breakdown.signals}. "
        f"Scores: ecs={decision.score_breakdown.ecs_score:.2f}, "
        f"lambda={decision.score_breakdown.lambda_score:.2f}, "
        f"ec2={decision.score_breakdown.ec2_score:.2f}. "
        "Do not invent facts beyond what is given; do not mention alternative "
        "numbers or change the decision."
    )


def _cost_prompt(decision: DecisionResult, cost: CostEstimateResult) -> str:
    return (
        "In 2-3 plain-English sentences for a non-technical, budget-conscious "
        f"reader, summarize this AWS cost estimate: compute_type="
        f"'{decision.compute_type}', estimated monthly cost={cost.amount} "
        f"{cost.currency} (range {cost.range_min}-{cost.range_max}). "
        "Do not invent a different number."
    )


def _finops_prompt(finops: FinOpsResult) -> str:
    discarded = ", ".join(f"{o.name} ({o.reason})" for o in finops.discarded_options)
    return (
        "In 2-3 plain-English sentences, justify why "
        f"'{finops.recommended_option}' is the recommended AWS pricing "
        f"optimization, given this context: {finops.context_used}. "
        f"The discarded alternatives and their reasons were: {discarded}. "
        "Do not recommend a different option than the one given."
    )


# --- fallback templates (used whenever Gemini is unavailable or fails) -----


def _fallback_architecture_explanation(decision: DecisionResult) -> str:
    scores = decision.score_breakdown
    return (
        f"{decision.compute_type.upper()} was selected based on the detected stack "
        f"signals (ECS score={scores.ecs_score:.1f}, Lambda score={scores.lambda_score:.1f}, "
        f"EC2 score={scores.ec2_score:.1f}). " + " ".join(decision.reasoning)
    )


def _fallback_cost_summary(decision: DecisionResult, cost: CostEstimateResult) -> str:
    return (
        f"Estimated monthly cost for this {decision.compute_type.upper()} deployment is "
        f"{cost.amount:.2f} {cost.currency} (range: {cost.range_min:.2f}-"
        f"{cost.range_max:.2f} {cost.currency})."
    )


def _fallback_finops_justification(finops: FinOpsResult) -> str:
    discarded_names = ", ".join(o.name for o in finops.discarded_options)
    return (
        f"Recommended cost optimization: '{finops.recommended_option}'. Alternatives "
        f"considered and discarded: {discarded_names}."
    )


# --- Gemini call plumbing ---------------------------------------------------


def _call_gemini_sync(prompt: str, *, timeout_seconds: float) -> str:
    """Runs a single Gemini call synchronously with a hard timeout.

    The shared GeminiClient (src/shared/llm/gemini) is imported lazily here,
    not at module load time, so this whole module still imports cleanly even
    if the optional `google-generativeai` dependency isn't installed — that
    case simply surfaces as an ImportError, caught by the caller's broad
    except alongside every other failure mode (network, quota, empty
    response, ...).
    """
    from src.shared.llm.gemini import GeminiClient  # noqa: PLC0415 - intentionally lazy

    async def _call() -> str:
        client = GeminiClient()
        response = await asyncio.wait_for(client.generate(prompt), timeout=timeout_seconds)
        return response.text

    return asyncio.run(_call())


def _generate_text(
    prompt: str,
    fallback_text: str,
    *,
    timeout_seconds: float,
) -> tuple[str, EnrichmentSource]:
    """Returns (text, source). Tries Gemini only if GEMINI_API_KEY is set;
    any failure at all (missing dependency, network, timeout, empty
    response) falls back to `fallback_text` instead of raising."""
    if not os.environ.get("GEMINI_API_KEY"):
        return fallback_text, "fallback"

    try:
        text = _call_gemini_sync(prompt, timeout_seconds=timeout_seconds)
        if not text or not text.strip():
            raise ValueError("Gemini returned an empty response")
        return text.strip(), "gemini"
    except Exception as exc:  # noqa: BLE001 - must never propagate past this module
        logger.warning("Gemini call failed (%s); using fallback text instead.", exc)
        return fallback_text, "fallback"


# --- public API --------------------------------------------------------------


def explain_architecture_decision(
    decision: DecisionResult, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[str, EnrichmentSource]:
    """Explains decision_engine's compute_type choice in plain English."""
    return _generate_text(
        _architecture_prompt(decision),
        _fallback_architecture_explanation(decision),
        timeout_seconds=timeout_seconds,
    )


def summarize_cost_estimation(
    decision: DecisionResult,
    cost: CostEstimateResult,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, EnrichmentSource]:
    """Summarizes cost_estimator's result for a non-technical reader."""
    return _generate_text(
        _cost_prompt(decision, cost),
        _fallback_cost_summary(decision, cost),
        timeout_seconds=timeout_seconds,
    )


def explain_finops_choice(
    finops: FinOpsResult, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> tuple[str, EnrichmentSource]:
    """Justifies finops_optimizer's recommended option in plain English."""
    return _generate_text(
        _finops_prompt(finops),
        _fallback_finops_justification(finops),
        timeout_seconds=timeout_seconds,
    )


def enrich(
    *,
    decision: DecisionResult,
    cost: CostEstimateResult,
    finops: FinOpsResult,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Enrichment:
    """Builds the full Enrichment block for the output contract.

    `enrichment_source` is "gemini" only if all three pieces of text came
    from Gemini; if even one fell back to a template, the whole block is
    conservatively marked "fallback" rather than overclaiming.
    """
    architecture_text, arch_source = explain_architecture_decision(
        decision, timeout_seconds=timeout_seconds
    )
    cost_text, cost_source = summarize_cost_estimation(
        decision, cost, timeout_seconds=timeout_seconds
    )
    finops_text, finops_source = explain_finops_choice(finops, timeout_seconds=timeout_seconds)

    overall_source: EnrichmentSource = (
        "gemini" if {arch_source, cost_source, finops_source} == {"gemini"} else "fallback"
    )

    return Enrichment(
        architecture_explanation=architecture_text,
        cost_summary=cost_text,
        finops_justification=finops_text,
        enrichment_source=overall_source,
    )
