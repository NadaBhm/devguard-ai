"""LLM-driven deployment context (Phase C): fill TerraformContext.region/environment.

Previously hardcoded "us-east-1"/"dev"; nothing else changes — compute_type is
decided elsewhere and other template variables are naming conventions, not decisions.
Validation-first (closed region/env lists); any failure silently keeps today's
defaults. database passes straight through — a module-1 fact, not a decision.
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

# In sync with data/aws_pricing.json's "region_multipliers" keys — a region outside
# this list leaves region_comparator.py with no price data.
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
    """Strict shape for the LLM's raw JSON response; anything outside the known
    region/environment lists fails validation -> deterministic default.
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
    health_check_port: int | None = None,
) -> TerraformContext:
    """Build a TerraformContext, letting an LLM pick region/environment when available
    and valid, otherwise today's fixed defaults. job_id/docker_image/source_code_path/
    health_check_port pass straight through — only region and environment are decided
    here."""
    region: AwsRegion = _DEFAULT_REGION
    environment: DeploymentEnvironment = _DEFAULT_ENVIRONMENT

    # DEVGUARD_AWS_REGION always wins: an LLM picking eu-west-1 while standing
    # resources sit in us-east-1 yields a payload DeployOps can't apply (no VPC).
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

    # sqlite is a local file — no RDS to provision or wire.
    db = analysis.stack_detection.database
    if db == "sqlite":
        db = None
    return TerraformContext(
        job_id=job_id,
        region=region,
        environment=environment,
        docker_image=docker_image,
        source_code_path=source_code_path,
        health_check_port=health_check_port,
        account_id=analysis.account_id,
        database=db,
    )
