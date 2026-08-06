"""
DevGuard AI - Orchestrator Chat (T-3.10 / T-3.11)
===================================================
Conversational interface over a running or finished analysis job.

CDC Reference:
    US-2.2.5 "As a user, I want to chat with the orchestrator so that I can
    ask questions during analysis. Given an active job, When I send a
    message, Then the LLM responds in < 2s using job context and RAG
    retrieval."

Note the two distinct sources in that acceptance criterion - "job context"
AND "RAG retrieval". They answer different questions:

  - RAG (Nada, src/lib/rag) has embedded the repo's README, docs and a
    sample of its code. It can answer "what framework does this use?".
  - The orchestrator state knows what the ANALYSIS found: the security
    score, the CVEs, the monthly cost estimate, whether deployment
    succeeded. RAG has never seen any of that - it indexed the repository,
    not the pipeline results. Ask a RAG-only chat "how much will this cost
    per month?" and it cannot answer.

So this module assembles both into one prompt.

WHY NOT JUST CALL ask_about_repo()?
-----------------------------------
lib.rag.api exposes ask_about_repo(job_id, question) which does retrieval
AND generation in one shot. It's the obvious entry point, but it is
stateless by construction: lib/rag/llm.py builds its prompt from
(context, query) only, with no room for conversation history or job facts.
Using it would make every message a cold start - "and what about the
second one?" would be unanswerable.

We therefore use its sibling, get_repo_context(), which Nada documented for
exactly this ("debug / custom prompting"): it returns the retrieved chunks
WITHOUT calling the LLM, letting us build the full prompt ourselves.

Owner: Hbib (Subgroup 2 - Execution & Control)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from .state import OrchestratorState

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION
# =============================================================================

# How many messages (user + assistant, counted individually) to carry into
# the prompt. 10 = roughly 5 exchanges. This is a latency/quality tradeoff,
# not a storage limit: every extra turn is extra tokens on every subsequent
# call, and US-2.2.5 budgets the whole round-trip at under 2 seconds.
MAX_HISTORY_MESSAGES = 10

# Chunks pulled from Qdrant per question.
DEFAULT_TOP_K = 5


def use_real_rag() -> bool:
    """Whether to call Nada's real RAG stack (Qdrant + Gemini)."""
    if os.getenv("DEVGUARD_REAL_AGENTS", "").strip() in ("1", "true", "True"):
        return True
    return os.getenv("DEVGUARD_REAL_RAG", "").strip() in ("1", "true", "True")


# =============================================================================
# T-3.11: CONVERSATION MEMORY
# =============================================================================

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
    message until it blows the model's context window (and the 2s budget).
    Older messages are dropped rather than summarized - summarizing would
    cost an extra LLM round-trip per message, which is the one thing this
    latency budget cannot afford.
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
        """Render the history for inclusion in a prompt."""
        if not self.messages:
            return ""
        lines = [
            f"{'User' if m.role == 'user' else 'Assistant'}: {m.content}"
            for m in self.messages
        ]
        return "\n".join(lines)

    def to_list(self) -> list[dict[str, str]]:
        """Serializable history, for the API / dashboard."""
        return [m.to_dict() for m in self.messages]

    def clear(self) -> None:
        self.messages.clear()


# Process-local store, keyed by job_id.
#
# Same lifetime and same caveat as the graph's MemorySaver: it lives in the
# FastAPI process and is lost on restart. That's acceptable for Sprint 3 and
# consistent with how job checkpoints already behave.
# TODO Sprint 5: persist alongside job state in PostgreSQL, in the same move
# that swaps MemorySaver for PostgresSaver.
_conversations: dict[str, ConversationMemory] = {}


def get_conversation(job_id: str) -> ConversationMemory:
    """Return (creating if needed) the conversation memory for a job."""
    if job_id not in _conversations:
        _conversations[job_id] = ConversationMemory(job_id=job_id)
    return _conversations[job_id]


def clear_conversation(job_id: str) -> None:
    _conversations.pop(job_id, None)


def reset_all_conversations() -> None:
    """Wipe every stored conversation (used by tests)."""
    _conversations.clear()


# =============================================================================
# JOB CONTEXT
# =============================================================================

def build_job_context(state: Optional[OrchestratorState]) -> str:
    """
    Summarize what the pipeline has learned so far, in plain text.

    This is the half of the prompt RAG cannot provide. Only sections that
    actually exist are included: mid-analysis there is no cost estimate yet,
    and inventing empty headings just wastes tokens and invites the model to
    hallucinate values for them.
    """
    if not state:
        return ""

    parts: list[str] = [
        f"Repository: {state.get('repo_url', 'unknown')}",
        f"Pipeline status: {state.get('status', 'unknown')}",
    ]

    codesec = state.get("codesec_result") or {}
    if codesec:
        score = codesec.get("security_score", {})
        summary = codesec.get("summary", {})
        stack = codesec.get("stack_detection", {})
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
        cost = infracost.get("cost_estimate", {})
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

    gates = state.get("human_gates") or {}
    pending = [
        name for name, gate in gates.items()
        if gate.get("required") and gate.get("approved") is None
    ]
    if pending:
        parts.append(f"Awaiting human approval at: {', '.join(pending)}.")

    return "\n".join(parts)


# =============================================================================
# PROMPT ASSEMBLY
# =============================================================================

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
    """Assemble the four blocks into the final prompt sent to the LLM."""
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


# =============================================================================
# T-3.10: CHAT ENTRY POINT
# =============================================================================

def chat(
    job_id: str,
    message: str,
    state: Optional[OrchestratorState] = None,
    *,
    top_k: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """
    Answer one chat message about a job.

    Args:
        job_id: job whose analysis (and Qdrant collection) to talk about.
        message: the user's question, in any language.
        state: current orchestrator state, for the pipeline-results half of
            the prompt. Optional - without it the assistant can still answer
            repository questions from RAG alone.
        top_k: how many repo chunks to retrieve.

    Returns:
        {"answer", "job_id", "history", "used_rag", "used_job_context"}

    The user message is recorded in history before the LLM call, and the
    answer after it, so a failed call doesn't silently drop the question
    from the conversation.
    """
    if not message or not message.strip():
        raise ValueError("Chat message cannot be empty")

    memory = get_conversation(job_id)
    history_block = memory.as_prompt_block()   # BEFORE appending the new message
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
    """Pull repo chunks from Nada's RAG. Never fatal: chat degrades to job context."""
    if not use_real_rag():
        logger.info("[%s] Chat: RAG in MOCK mode (set DEVGUARD_REAL_RAG=1 for real)", job_id)
        return _MOCK_REPO_CONTEXT

    try:
        from lib.rag.api import get_repo_context  # lazy: branch may not have it

        return get_repo_context(job_id=job_id, question=question, top_k=top_k) or ""
    except Exception as exc:
        # A cold Qdrant, a missing collection (job never ingested) or a
        # network blip must not take the chat down - the pipeline results
        # alone still answer a lot of questions.
        logger.warning("[%s] RAG retrieval failed, continuing without it: %s", job_id, exc)
        return ""


def _generate(prompt: str, *, job_id: str) -> str:
    """Send the assembled prompt to the LLM."""
    if not use_real_rag():
        return _mock_answer(prompt)

    try:
        from lib.rag.llm import GeminiClient  # lazy

        return GeminiClient().query(prompt)
    except Exception as exc:
        logger.error("[%s] Chat LLM call failed: %s", job_id, exc)
        return (
            "I couldn't reach the language model just now, so I can't answer "
            "that yet. The analysis results themselves are unaffected - please "
            "try again in a moment."
        )


# =============================================================================
# MOCK MODE
# =============================================================================

_MOCK_REPO_CONTEXT = (
    "README.md: DevGuard AI - an agentic DevSecOps platform that analyzes a "
    "public GitHub repository, audits its security, generates Terraform, "
    "estimates AWS cost and deploys it.\n"
    "---\n"
    "requirements.txt: fastapi==0.110.0, langgraph, celery, redis, qdrant-client"
)


def _mock_answer(prompt: str) -> str:
    """
    Deterministic stand-in for the LLM.

    Echoes back which prompt sections were assembled, so the wiring (memory,
    job context, retrieval) can be tested end-to-end without a Gemini API key
    or a running Qdrant.
    """
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
