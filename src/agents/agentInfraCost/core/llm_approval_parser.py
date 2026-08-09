"""Phase D (post-mission extension): parse a client's free-text approval reply.

At the human-approval gate (between InfraCost and deployment), a client may
answer in plain language — "ok go ahead", "do it but in Europe", "no, too
expensive" — rather than clicking a fixed approve/reject button. This module
turns that free text into a strict, validated decision: approved, rejected,
or unclear, plus an optional region if one was mentioned.

Same validation-first pattern as llm_architecture_advisor.py /
llm_deployment_advisor.py: the LLM may only pick from closed values —
{"approved", "rejected", "unclear"} and the three known regions — never
free-form. Unlike those two modules, there is no deterministic fallback
*algorithm* to fall back to here (there's no scoring system for "what did
this sentence mean"); instead, any failure (no OPENROUTER_API_KEY, a failed
call, a malformed response) resolves to status="unclear" — NEVER
"approved". A deployment must never be approved based on a guess or a
parsing failure; "unclear" tells the caller to go back and ask the human
again, exactly like a genuine ambiguous answer would.

Not yet wired into the orchestrator (src/subgroup2/orchestrator/graph.py) —
same reason as core/orchestrator_adapter.py: that file's location is about
to change on this branch (a teammate's already-merged move on master), so
wiring it now would need to be redone after rebasing.
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
    """The result of interpreting a client's free-text approval reply.

    ``status`` is deliberately never defaulted to "approved" by this
    module under any failure condition — see the module docstring.
    """

    status: ApprovalStatus
    region: AwsRegion | None = None
    reasoning: str


class _LlmApprovalChoice(BaseModel):
    """Strict shape the LLM's raw JSON response must match. Anything else —
    malformed JSON, a missing field, a status/region outside the known
    values — fails validation and resolves to "unclear", not a guess.
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

    Args:
        client_text: The client's raw message, e.g. "ok go ahead", "do it
            but in Europe", "no, too expensive".

    Returns:
        An ``ApprovalDecision``. ``status`` is "unclear" whenever
        ``OPENROUTER_API_KEY`` is unset, the call fails or times out, or
        the response is invalid — never "approved" on a failure. Callers
        must treat "unclear" as "ask the human again", not as a rejection
        or an approval.
    """
    raw_text = call_llm(prompt=client_text, system_instruction=_SYSTEM_INSTRUCTION)
    if raw_text is None:
        return ApprovalDecision(status="unclear", reasoning=_UNCLEAR_FALLBACK_REASONING)

    choice = _parse_llm_choice(raw_text)
    if choice is None:
        return ApprovalDecision(status="unclear", reasoning=_UNCLEAR_FALLBACK_REASONING)

    return ApprovalDecision(status=choice.status, region=choice.region, reasoning=choice.reasoning)
