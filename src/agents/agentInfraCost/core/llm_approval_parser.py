"""Phase D: parse a client's free-text approval reply into a strict decision.

Plain-language replies become approved/rejected/unclear + an optional known region,
same validation-first pattern as siblings; ANY failure resolves to "unclear", never
"approved" (a guess must never deploy) — callers re-ask. Not orchestrator-wired yet.
"""

from __future__ import annotations

import json
import logging
from typing import Final, Literal

from pydantic import BaseModel, ValidationError

from core.llm_deployment_advisor import AwsRegion
from core.llm_provider import call_llm

logger = logging.getLogger(__name__)

ApprovalStatus = Literal["approved", "rejected", "unclear"]

_SYSTEM_INSTRUCTION: Final[str] = (
    "Tu interprètes la réponse en langage libre d'un client à une demande "
    "d'approbation de déploiement. Détermine s'il approuve, refuse, ou si "
    "ce n'est pas clair. S'il mentionne une région parmi 'us-east-1', "
    "'eu-west-1', 'ap-southeast-1', indique-la — sinon laisse ce champ à "
    "null, ne devine jamais une région qui n'est pas explicitement dite. "
    "Réponds uniquement avec un JSON de la forme "
    '{"status": "approved"|"rejected"|"unclear", "region": "..."|null, '
    '"reasoning": "..."}, sans texte autour. Utilise "unclear" dès que le '
    "texte n'exprime pas clairement une décision."
)

_UNCLEAR_FALLBACK_REASONING: Final[str] = (
    "La réponse n'a pas pu être interprétée automatiquement — "
    "une clarification humaine est nécessaire avant de continuer."
)


class ApprovalDecision(BaseModel):
    """The result of interpreting a client's free-text reply; status is never
    defaulted to "approved" under any failure condition.
    """

    status: ApprovalStatus
    region: AwsRegion | None = None
    reasoning: str


class _LlmApprovalChoice(BaseModel):
    """Strict shape for the LLM's raw JSON response; anything outside the known
    values fails validation and resolves to "unclear", not a guess.
    """

    status: ApprovalStatus
    region: AwsRegion | None = None
    reasoning: str


def _parse_llm_choice(raw_text: str) -> _LlmApprovalChoice | None:
    try:
        payload = json.loads(raw_text)
        return _LlmApprovalChoice.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        logger.warning("LLM approval-parsing response failed validation: %s", exc)
        return None


def parse_approval_response(client_text: str) -> ApprovalDecision:
    """Interpret a client's free-text reply to an approval request.

    Status is "unclear" whenever OPENROUTER_API_KEY is unset, the call fails/times
    out, or the response is invalid — never "approved" on a failure. Callers must
    treat "unclear" as "ask the human again", not a rejection."""
    raw_text = call_llm(prompt=client_text, system_instruction=_SYSTEM_INSTRUCTION)
    if raw_text is None:
        return ApprovalDecision(status="unclear", reasoning=_UNCLEAR_FALLBACK_REASONING)

    choice = _parse_llm_choice(raw_text)
    if choice is None:
        return ApprovalDecision(status="unclear", reasoning=_UNCLEAR_FALLBACK_REASONING)

    return ApprovalDecision(status=choice.status, region=choice.region, reasoning=choice.reasoning)
