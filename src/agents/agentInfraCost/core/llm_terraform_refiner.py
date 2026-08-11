"""Feedback-driven Terraform refinement via an LLM.

Runs after ``generate_terraform`` (module 3) when the orchestrator's Gate 2
asked for changes (``RepoAnalysisInput.user_feedback`` is set). The LLM edits
the already-rendered ``main.tf`` / ``variables.tf`` / ``outputs.tf`` to honor
the user's request — e.g. "make it cheaper", "use two AZs", "swap to
Graviton" — and returns all three files as validated HCL strings.

Design contract (mirrors ``llm_architecture_advisor`` / ``llm_deployment_advisor``):

- The LLM's usable surface is exactly three string fields — it may not add
  files, change the architecture decision, or invent resources outside the
  existing file set. Output is validated with a strict Pydantic shape, and
  only files that parse are accepted.
- Fail-soft: every failure mode (no ``OPENROUTER_API_KEY``, network error,
  timeout, non-JSON reply, malformed/empty HCL, Pydantic rejection) returns
  the ORIGINAL files unchanged — never an error, never a partial write. The
  pipeline must be able to proceed even if the refiner is unavailable.
- The architecture (``compute_type``, sizing) is decided elsewhere
  (``llm_architecture_advisor``); this module never touches it.

Uses ``core.llm_provider.call_llm`` (OpenRouter), which honours
``OPENROUTER_MODEL`` — set to ``nvidia/nemotron-3-ultra-550b-a55b:free`` in
the default environment.
"""

from __future__ import annotations

import json
import logging
from typing import Final

from pydantic import BaseModel, ValidationError

from core.llm_provider import call_llm
from models.output_schema import TerraformFiles

logger = logging.getLogger(__name__)

_SYSTEM_INSTRUCTION: Final[str] = (
    "Tu es un ingénieur Terraform senior. On te donne les trois fichiers "
    "Terraform d'une infrastructure AWS et une demande de modification de "
    "l'utilisateur. Réécris UNIQUEMENT les fichiers concernés pour satisfaire "
    "la demande, en gardant tout le reste strictement identique. "
    "Réponds uniquement avec un JSON de la forme "
    '{"main_tf": "...", "variables_tf": "...", "outputs_tf": "..."}, sans texte '
    "autour. Chaque valeur doit être le contenu HCL complet du fichier, échappé "
    "dans la chaîne JSON. Ne modifie ni la configuration de l'architecture ni "
    "les valeurs de dimensionnement déjà décidées, sauf si la demande le dit "
    "explicitement."
)


class _RefinedTerraform(BaseModel):
    """Strict shape the LLM's raw JSON response must match. Anything else —
    malformed JSON, a missing field, or an empty file — triggers the
    fail-soft fallback (original files unchanged).
    """

    main_tf: str
    variables_tf: str
    outputs_tf: str


def _build_prompt(current: TerraformFiles, feedback: str) -> str:
    return (
        "=== main.tf ===\n"
        f"{current.main_tf}\n\n"
        "=== variables.tf ===\n"
        f"{current.variables_tf}\n\n"
        "=== outputs.tf ===\n"
        f"{current.outputs_tf}\n\n"
        "=== DEMANDE DE L'UTILISATEUR ===\n"
        f"{feedback}\n\n"
        "Renvoie les trois fichiers modifiés en JSON."
    )


def _parse_llm_output(raw_text: str | None) -> _RefinedTerraform | None:
    if not raw_text:
        return None
    try:
        payload = json.loads(raw_text)
        return _RefinedTerraform.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        logger.warning("LLM Terraform refinement failed validation: %s", exc)
        return None


def _valid_file(content: str) -> bool:
    """A refined file must be non-empty; anything else is a refused edit."""
    return bool(content and content.strip())


def refine_terraform(
    current: TerraformFiles,
    feedback: str,
) -> TerraformFiles:
    """Refine the rendered Terraform files from a user prompt.

    Args:
        current: the Terraform files produced by ``generate_terraform``.
        feedback: the user's free-form change request from Gate 2.

    Returns:
        A new ``TerraformFiles`` honoring the request, or — on any LLM
        failure or invalid output — ``current`` unchanged (fail-soft, same
        contract as every other LLM call in this agent).
    """
    raw_text = call_llm(
        prompt=_build_prompt(current, feedback),
        system_instruction=_SYSTEM_INSTRUCTION,
    )
    refined = _parse_llm_output(raw_text)
    if refined is None:
        logger.info("Terraform refiner unavailable or invalid; keeping original files")
        return current

    if not (_valid_file(refined.main_tf) and _valid_file(refined.variables_tf) and _valid_file(refined.outputs_tf)):
        logger.warning("Terraform refiner returned empty files; keeping originals")
        return current

    logger.info("Terraform refiner applied user feedback")
    return TerraformFiles(
        main_tf=refined.main_tf,
        variables_tf=refined.variables_tf,
        outputs_tf=refined.outputs_tf,
    )
