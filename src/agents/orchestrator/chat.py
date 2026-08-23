"""
DevGuard AI - Orchestrator Chat
Conversational interface over a running or finished analysis job.

RAG (lib.rag) knows the repo's README and code samples.
The orchestrator state knows the analysis results: security score, CVEs,
cost estimate, deployment status. RAG alone cannot answer "how much will
this cost per month?" because it indexed the repository, not the pipeline
results. This module assembles both into one prompt.

We do NOT use ask_about_repo() because it is stateless: it builds its
prompt from (context, query) only, with no room for conversation history
or job facts. Every message would be a cold start. Instead we use its
sibling get_repo_context(), which returns retrieved chunks without calling
the LLM, letting us build the full prompt ourselves.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional
from typing import Any, Literal, Optional, cast
from .state import OrchestratorState

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 10
DEFAULT_TOP_K = 5


def use_real_rag() -> bool:
    if os.getenv("DEVGUARD_REAL_AGENTS", "").strip() in ("1", "true", "True"):
        return True
    return os.getenv("DEVGUARD_REAL_RAG", "").strip() in ("1", "true", "True")


Role = Literal["user", "assistant"]


@dataclass
class ChatMessage:
    role: Role
    content: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content, "timestamp": self.timestamp}


@dataclass
class ConversationMemory:
    """
    Rolling conversation history for one job.

    Bounded on purpose: an unbounded history would grow the prompt on every
    message until it blows the model's context window (and the latency budget).
    Older messages are dropped rather than summarized — summarizing would cost
    an extra LLM round-trip per message, which the budget cannot afford.
    """

    job_id: str
    messages: list[ChatMessage] = field(default_factory=list)
    max_messages: int = MAX_HISTORY_MESSAGES

    def append(self, role: Role, content: str) -> None:
        self.messages.append(ChatMessage(role=role, content=content))
        overflow = len(self.messages) - self.max_messages
        if overflow > 0:
            del self.messages[:overflow]

    def as_prompt_block(self) -> str:
        if not self.messages:
            return ""
        lines = [
            f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}"
            for m in self.messages
        ]
        return "\n".join(lines)

    def to_list(self) -> list[dict[str, str]]:
        return [m.to_dict() for m in self.messages]

    def clear(self) -> None:
        self.messages.clear()


_conversations: dict[str, ConversationMemory] = {}


def get_conversation(job_id: str) -> ConversationMemory:
    if job_id not in _conversations:
        _conversations[job_id] = ConversationMemory(job_id=job_id)
    return _conversations[job_id]


def clear_conversation(job_id: str) -> None:
    _conversations.pop(job_id, None)


def reset_all_conversations() -> None:
    _conversations.clear()


def build_job_context(state: Optional[OrchestratorState]) -> str:
    if not state:
        return ""

    parts: list[str] = [
        f"Repository: {state.get('repo_url', 'unknown')}",
        f"Pipeline status: {state.get('status', 'unknown')}",
    ]

    codesec = state.get("codesec_result") or {}
    if codesec:
        score = codesec.get("security_score") or {}
        summary = codesec.get("summary") or {}
        stack = codesec.get("stack_detection") or {}
        parts.append(
            "Security analysis: score {}/100 (grade {}), {} critical / {} high findings, "
            "{} hardcoded secrets, {} vulnerable dependencies.".format(
                score.get("score", "?"),
                score.get("grade", "?"),
                summary.get("total_critical", 0),
                summary.get("total_high", 0),
                summary.get("secrets_found_count", 0),
                summary.get("vulnerable_dependencies_count", 0),
            )
        )
        if stack:
            parts.append(
                "Detected stack: {} / {} / database: {}.".format(
                    stack.get("primary_language", "?"),
                    ", ".join(stack.get("frameworks", [])) or "no framework detected",
                    stack.get("database", "none"),
                )
            )
        recommendations = score.get("recommendations") or []
        if recommendations:
            parts.append(
                "Top security recommendations: " + "; ".join(recommendations[:4])
            )

    infracost = state.get("infracost_result") or {}
    if infracost:
        cost = infracost.get("cost_estimate") or {}
        parts.append(
            "Infrastructure: {} recommended, estimated ${}/month.".format(
                infracost.get("architecture_recommendation", "?"),
                cost.get("monthly_cost_usd", "?"),
            )
        )
        breakdown = cost.get("breakdown") or []
        if breakdown:
            parts.append(
                "Cost breakdown: "
                + ", ".join(
                    f"{item.get('service')} ${item.get('monthly_cost_usd')}"
                    for item in breakdown
                )
            )

    deployops = state.get("deployops_result") or {}
    if deployops:
        parts.append(
            "Deployment: {} at {}.".format(
                deployops.get("deployment_status", "?"),
                deployops.get("deployed_url") or "no URL",
            )
        )
        if deployops.get("rollback_triggered"):
            parts.append(
                f"A rollback was triggered: {deployops.get('rollback_reason', 'unknown reason')}."
            )

    gates = cast(dict[str, Any], state["human_gates"])
    pending = [
        name for name, gate in gates.items()
        if gate.get("required") and gate.get("approved") is None
    ]
    if pending:
        parts.append(f"Awaiting human approval at: {', '.join(pending)}.")

    return "\n".join(parts)


_SYSTEM_PREAMBLE = (
    "You are DevGuard AI's assistant. You help a developer understand the "
    "security analysis, infrastructure cost estimate and deployment of their "
    "GitHub repository.\n"
    "Answer using ONLY the pipeline results and repository excerpts provided "
    "below. If something is not covered by them, say so plainly instead of "
    "guessing - never invent a score, a cost figure or a URL.\n"
    "Be concise and concrete. Answer in the language the user writes in."
)


def build_prompt(
    question: str,
    *,
    job_context: str,
    repo_context: str,
    history: str,
) -> str:
    sections = [_SYSTEM_PREAMBLE]

    if job_context:
        sections.append(f"=== PIPELINE RESULTS ===\n{job_context}")
    if repo_context:
        sections.append(f"=== REPOSITORY EXCERPTS ===\n{repo_context}")
    if history:
        sections.append(
            "=== CONVERSATION SO FAR ===\n"
            f"{history}\n"
            "(Use this to resolve references like \"it\", \"that one\", \"the second one\".)"
        )

    sections.append(f"=== QUESTION ===\n{question}")
    sections.append("Answer:")
    return "\n\n".join(sections)


def chat(
    job_id: str,
    message: str,
    state: Optional[OrchestratorState] = None,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    if not message or not message.strip():
        raise ValueError("Chat message cannot be empty")

    memory = get_conversation(job_id)
    history_block = memory.as_prompt_block()
    memory.append("user", message)

    job_context = build_job_context(state)
    repo_context = _retrieve_repo_context(job_id, message, top_k)

    prompt = build_prompt(
        message,
        job_context=job_context,
        repo_context=repo_context,
        history=history_block,
    )

    answer = _generate(prompt, job_id=job_id)
    memory.append("assistant", answer)

    return {
        "job_id": job_id,
        "answer": answer,
        "history": memory.to_list(),
        "used_rag": bool(repo_context),
        "used_job_context": bool(job_context),
    }


def _retrieve_repo_context(job_id: str, question: str, top_k: int) -> str:
    if not use_real_rag():
        logger.info("[%s] Chat: RAG in MOCK mode (set DEVGUARD_REAL_RAG=1 for real)", job_id)
        return _MOCK_REPO_CONTEXT

    try:
        from lib.rag.api import get_repo_context
        return get_repo_context(job_id=job_id, question=question, top_k=top_k) or ""
    except Exception as exc:
        logger.warning("[%s] RAG retrieval failed, continuing without it: %s", job_id, exc)
        return ""


def _generate(prompt: str, *, job_id: str) -> str:
    if not use_real_rag():
        return _mock_answer(prompt)

    try:
        from lib.rag.llm import GeminiClient
        return GeminiClient().query(prompt)
    except Exception as exc:
        logger.error("[%s] Chat LLM call failed: %s", job_id, exc)
        return (
            "I couldn't reach the language model just now, so I can't answer "
            "that yet. The analysis results themselves are unaffected - please "
            "try again in a moment."
        )


_MOCK_REPO_CONTEXT = (
    "README.md: DevGuard AI - an agentic DevSecOps platform that analyzes a "
    "public GitHub repository, audits its security, generates Terraform, "
    "estimates AWS cost and deploys it.\n"
    "---\n"
    "requirements.txt: fastapi==0.110.0, langgraph, celery, redis, qdrant-client"
)


def _mock_answer(prompt: str) -> str:
    blocks = [
        name
        for marker, name in (
            ("=== PIPELINE RESULTS ===", "pipeline results"),
            ("=== REPOSITORY EXCERPTS ===", "repository excerpts"),
            ("=== CONVERSATION SO FAR ===", "conversation history"),
        )
        if marker in prompt
    ]
    question = prompt.rsplit("=== QUESTION ===\n", 1)[-1].rsplit("\n\nAnswer:", 1)[0]
    sources = ", ".join(blocks) if blocks else "no context"
    return f"[MOCK] Answering {question!r} using: {sources}."