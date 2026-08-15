"""Phase C (post-mission extension): LLM-driven deployment context.

Fills in the two ``TerraformContext`` fields no other module ever decides —
``region`` and ``environment`` — previously always hardcoded to
``"us-east-1"`` and ``"dev"``. Nothing else about Terraform generation
changes: ``compute_type`` (which of the 9 templates gets used) is already
decided by ``llm_architecture_advisor.py`` (Phase B), and every other
template variable (``cluster_name``, ``ami_id``, ``handler``, ...) is a
naming convention, not a decision — letting an LLM invent those would be
exactly the "uncontrolled generation" this design avoids. The 9 Jinja2
templates in ``terraform_generator.py`` are never touched.

Same validation-first pattern as ``llm_architecture_advisor.py``: the LLM
may only pick from a closed list of known-safe values — the regions
``data/aws_pricing.json``'s ``region_multipliers`` already prices (see
``region_comparator.py``), plus the three standard environment tiers. Any
failure — no ``OPENROUTER_API_KEY``, a failed/timed-out call, malformed
JSON, a value outside the allowed lists — falls back to today's fixed
defaults, automatically and silently.

Also passes ``analysis.stack_detection.database`` straight through to
``TerraformContext.database`` (no LLM involved in this one — it's a plain
fact from module 1, not a decision). The ECS template uses it to declare
(never create) database connection variables — see
``terraform_generator.py``'s docstring.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Final, Literal

from pydantic import BaseModel, ValidationError

from core.llm_provider import call_llm
from core.terraform_generator import TerraformContext
from models.input_schema import RepoAnalysisInput

logger = logging.getLogger(__name__)

# Kept in sync with data/aws_pricing.json's "region_multipliers" keys —
# picking a region outside this list would mean the rest of the system
# (region_comparator.py) has no price data for it.
AwsRegion = Literal["us-east-1", "eu-west-1", "ap-southeast-1"]
DeploymentEnvironment = Literal["dev", "staging", "prod"]

_DEFAULT_REGION: Final[AwsRegion] = "us-east-1"
_DEFAULT_ENVIRONMENT: Final[DeploymentEnvironment] = "dev"

_SYSTEM_INSTRUCTION: Final[str] = (
    "Tu choisis deux paramètres de déploiement pour une infrastructure AWS : "
    "une région parmi 'us-east-1', 'eu-west-1', 'ap-southeast-1' — aucune autre "
    "valeur n'est acceptée — et un environnement parmi 'dev', 'staging', 'prod'. "
    "Réponds uniquement avec un JSON de la forme "
    '{"region": "...", "environment": "...", "reasoning": "..."}, sans texte autour.'
)


class _LlmDeploymentChoice(BaseModel):
    """Strict shape the LLM's raw JSON response must match. Anything else —
    malformed JSON, a missing field, a region/environment outside the known
    lists — fails validation and triggers the deterministic default.
    """

    region: AwsRegion
    environment: DeploymentEnvironment
    reasoning: str


def _build_prompt(analysis: RepoAnalysisInput) -> str:
    prompt = (
        "Signaux du dépôt :\n"
        f"- nom du dépôt : {analysis.repo_metadata.name}\n"
        f"- branche : {analysis.repo_metadata.branch}\n"
        f"- taille du projet (lignes de code) : {analysis.repo_metadata.loc}\n\n"
        "Quelle région AWS et quel environnement de déploiement recommandes-tu ?"
    )
    if analysis.user_feedback:
        prompt += (
            "\n\nContrainte supplémentaire de l'utilisateur (prioritaire) :\n"
            f"{analysis.user_feedback}"
        )
    if analysis.repo_context:
        prompt += (
            "\n\n=== CONTEXTE DU DÉPÔT (faits extraits par le LLM) ===\n"
            f"{analysis.repo_context}"
        )
    return prompt


def _parse_llm_choice(raw_text: str) -> _LlmDeploymentChoice | None:
    try:
        payload = json.loads(raw_text)
        return _LlmDeploymentChoice.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        logger.warning("LLM deployment-context response failed validation: %s", exc)
        return None


def decide_deployment_context(
    analysis: RepoAnalysisInput,
    *,
    job_id: str,
    docker_image: str | None,
    source_code_path: str | None = None,
) -> TerraformContext:
    """Build a TerraformContext, letting an LLM pick region/environment when
    available and valid, otherwise using today's fixed defaults.

    Args:
        analysis: The validated payload produced by ``input_validator``.
        job_id, docker_image, source_code_path: Passed straight through to
            ``TerraformContext`` — this module only ever decides ``region``
            and ``environment``.

    Returns:
        A ``TerraformContext`` with ``region``/``environment`` from the LLM
        if it returned a valid, allowed choice, otherwise
        ``"us-east-1"``/``"dev"`` — same defaults as before this module
        existed.
    """
    region: AwsRegion = _DEFAULT_REGION
    environment: DeploymentEnvironment = _DEFAULT_ENVIRONMENT

    # Deterministic override: the target AWS account lives in exactly one
    # region. InfraCost cannot see the account's real VPC/subnets, so an LLM
    # picking "eu-west-1" while the standing resources are all in us-east-1
    # produces a payload DeployOps cannot apply (VPC id doesn't exist there).
    # When the operator pins DEVGUARD_AWS_REGION, that value always wins over
    # the LLM's guess.
    pinned_region = os.getenv("DEVGUARD_AWS_REGION")
    if pinned_region:
        region = pinned_region
        logger.info("Region pinned by DEVGUARD_AWS_REGION=%s (LLM choice skipped)", pinned_region)
    else:
        raw_text = call_llm(prompt=_build_prompt(analysis), system_instruction=_SYSTEM_INSTRUCTION)
        if raw_text is not None:
            choice = _parse_llm_choice(raw_text)
            if choice is not None:
                region, environment = choice.region, choice.environment
                logger.info(
                    "LLM chose region=%s environment=%s: %s",
                    region, environment, choice.reasoning,
                )

    return TerraformContext(
        job_id=job_id,
        region=region,
        environment=environment,
        docker_image=docker_image,
        source_code_path=source_code_path,
        database=analysis.stack_detection.database,
    )
