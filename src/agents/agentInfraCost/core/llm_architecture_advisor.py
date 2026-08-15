"""Phase B (post-mission extension): LLM-driven architecture decision.

Asks an LLM (via ``core.llm_provider``, OpenRouter) to pick ``compute_type``
from the three supported values, given the same structural repo-analysis
signals ``decision_engine.py``'s scoring already uses. The LLM decides WHICH
of ecs/lambda/ec2 fits best and explains why in plain language; it never
invents sizing values — those still come from ``decision_engine``'s existing,
tested sizing tiers (``compute_sizing``), so nothing free-form ever reaches
Terraform generation. This is the validation layer against unrestricted
generation: the LLM's usable surface is exactly one ``Literal`` field out of
three known values, enforced by ``_LlmArchitectureChoice``.

Falls back to ``decide_architecture()``'s deterministic scoring —
automatically, silently, with identical downstream behaviour — whenever
``OPENROUTER_API_KEY`` is unset, the call fails or times out, the response
isn't valid JSON, or the LLM names a ``compute_type`` outside
{ecs, lambda, ec2}. ``decision_source`` always records which path actually
produced the result, so nothing is hidden from the rest of the pipeline.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from pydantic import BaseModel, ValidationError

from core.decision_engine import ComputeType, DecisionResult, compute_sizing, decide_architecture
from core.llm_provider import call_llm
from models.input_schema import RepoAnalysisInput

logger = logging.getLogger(__name__)

_SYSTEM_INSTRUCTION: Final[str] = (
    "Tu es un architecte cloud AWS. On te donne des signaux structurels sur un "
    "dépôt de code (présence d'un conteneur, d'un docker-compose, d'une base de "
    "données, de frameworks web, et sa taille en lignes de code). Choisis "
    "EXACTEMENT un type de compute parmi 'ecs', 'lambda' ou 'ec2' — aucune autre "
    "valeur n'est acceptée. Réponds uniquement avec un JSON de la forme "
    '{"compute_type": "...", "reasoning": "..."}, sans texte autour.\n'
    "\n"
    "RÈGLE ABSOLUE : Si le contexte inclut une 'Contrainte supplémentaire de l'utilisateur' "
    "(user feedback), cette contrainte EST PRIORITAIRE sur ton analyse structurelle. "
    "Si l'utilisateur demande explicitement 'ecs', 'lambda' ou 'ec2', TU DOIS "
    "respecter ce choix même s'il contredit ton analyse structurelle. La demande "
    "utilisateur EST UN ORDRE, pas une suggestion."
)


class _LlmArchitectureChoice(BaseModel):
    """Strict shape the LLM's raw JSON response must match. Anything else —
    malformed JSON, a missing field, a compute_type outside the three known
    values — fails validation and triggers the deterministic fallback.
    """

    compute_type: ComputeType
    reasoning: str


def _build_prompt(analysis: RepoAnalysisInput) -> str:
    stack = analysis.stack_detection
    prompt = (
        "Signaux du dépôt :\n"
        f"- conteneur détecté : {stack.container.detected}\n"
        f"- docker-compose détecté : {stack.container.compose_detected}\n"
        f"- base de données détectée : {stack.database is not None}\n"
        f"- au moins un framework détecté : {len(stack.frameworks) > 0}\n"
        f"- taille du projet (lignes de code) : {analysis.repo_metadata.loc}\n\n"
        "Quel type de compute recommandes-tu ?"
    )
    if analysis.user_feedback:
        prompt += (
            "\n\nContrainte supplémentaire de l'utilisateur (prioritaire sur tout "
            "le reste) :\n"
            f"{analysis.user_feedback}"
        )
    if analysis.repo_context:
        prompt += (
            "\n\n=== CONTEXTE DU DÉPÔT (faits extraits par le LLM) ===\n"
            f"{analysis.repo_context}"
        )
    return prompt


def _parse_llm_choice(raw_text: str) -> _LlmArchitectureChoice | None:
    try:
        payload = json.loads(raw_text)
        return _LlmArchitectureChoice.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        logger.warning("LLM architecture response failed validation: %s", exc)
        return None


def decide_architecture_via_llm(analysis: RepoAnalysisInput) -> DecisionResult:
    """Decide compute_type via LLM judgment, with automatic deterministic
    fallback.

    Args:
        analysis: The validated payload produced by ``input_validator``.

    Returns:
        A ``DecisionResult`` exactly like ``decide_architecture()``'s, plus
        ``decision_source`` ("llm" or "deterministic") and, when the LLM
        path succeeded, ``llm_reasoning`` holding its explanation.
        ``score_breakdown`` is always the deterministic score (kept as
        context), even when the LLM's pick differs from what the scores
        alone would choose — so the deterministic view is never hidden.
    """
    deterministic = decide_architecture(analysis)

    raw_text = call_llm(prompt=_build_prompt(analysis), system_instruction=_SYSTEM_INSTRUCTION)
    if raw_text is None:
        return deterministic

    choice = _parse_llm_choice(raw_text)
    if choice is None:
        return deterministic

    logger.info(
        "LLM chose compute_type=%s (deterministic scoring would have picked %s): %s",
        choice.compute_type, deterministic.compute_type, choice.reasoning,
    )
    sizing = compute_sizing(choice.compute_type, analysis)
    return DecisionResult(
        compute_type=choice.compute_type,
        sizing=sizing,
        score_breakdown=deterministic.score_breakdown,
        decision_source="llm",
        llm_reasoning=choice.reasoning,
    )
