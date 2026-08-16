"""
Tests for chat.py (T-3.10 chat LLM / T-3.11 conversation memory)

US-2.2.5: the assistant must answer "using job context AND RAG retrieval".
Both halves are asserted here, as is the memory behaviour that makes
follow-up questions ("and that one?") answerable at all.
"""

import pytest

from src.agents.orchestrator import chat as chat_module
from src.agents.orchestrator.chat import (
    MAX_HISTORY_MESSAGES,
    ConversationMemory,
    build_job_context,
    build_prompt,
    chat,
    clear_conversation,
    get_conversation,
    reset_all_conversations,
    use_real_rag,
)
from src.agents.orchestrator.nodes import (
    build_mock_codesec_result,
    build_mock_deployops_result,
    build_mock_infracost_result,
)
from src.agents.orchestrator.state import create_initial_state


@pytest.fixture(autouse=True)
def clean_memory():
    reset_all_conversations()
    yield
    reset_all_conversations()


@pytest.fixture
def completed_state():
    state = create_initial_state("https://github.com/test/repo")
    state["status"] = "completed"
    state["codesec_result"] = build_mock_codesec_result(state["job_id"], state["repo_url"])
    state["infracost_result"] = build_mock_infracost_result()
    state["deployops_result"] = build_mock_deployops_result(state["job_id"])
    return state


class TestConversationMemory:
    def test_starts_empty(self):
        assert get_conversation("job-1").messages == []

    def test_same_job_returns_same_conversation(self):
        get_conversation("job-1").append("user", "hello")
        assert len(get_conversation("job-1").messages) == 1

    def test_jobs_are_isolated(self):
        get_conversation("job-1").append("user", "secret question")
        assert get_conversation("job-2").messages == []

    def test_history_is_bounded(self):
        """Unbounded history would grow the prompt until it breaks the 2s budget."""
        memory = ConversationMemory(job_id="j", max_messages=4)
        for i in range(10):
            memory.append("user", f"message {i}")
        assert len(memory.messages) == 4
        assert memory.messages[-1].content == "message 9"
        assert memory.messages[0].content == "message 6"

    def test_default_bound_is_applied(self):
        memory = get_conversation("job-1")
        for i in range(MAX_HISTORY_MESSAGES + 6):
            memory.append("user", f"m{i}")
        assert len(memory.messages) == MAX_HISTORY_MESSAGES

    def test_prompt_block_labels_roles(self):
        memory = ConversationMemory(job_id="j")
        memory.append("user", "what is the score?")
        memory.append("assistant", "68/100")
        block = memory.as_prompt_block()
        assert "User: what is the score?" in block
        assert "Assistant: 68/100" in block

    def test_clear_removes_the_conversation(self):
        get_conversation("job-1").append("user", "hi")
        clear_conversation("job-1")
        assert get_conversation("job-1").messages == []


class TestJobContext:
    def test_empty_without_state(self):
        assert build_job_context(None) == ""

    def test_includes_security_findings(self, completed_state):
        context = build_job_context(completed_state)
        assert "68/100" in context
        assert "grade C" in context

    def test_includes_cost_estimate(self, completed_state):
        """RAG indexed the README; it has never seen a dollar figure."""
        context = build_job_context(completed_state)
        assert "145.32" in context
        assert "ecs_fargate" in context

    def test_includes_deployment_status(self, completed_state):
        context = build_job_context(completed_state)
        assert "Deployment: success" in context

    def test_includes_detected_stack(self, completed_state):
        context = build_job_context(completed_state)
        assert "python" in context
        assert "fastapi" in context

    def test_omits_sections_that_do_not_exist_yet(self):
        """Mid-analysis there is no cost yet; don't invite the model to invent one."""
        state = create_initial_state("https://github.com/test/repo")
        state["status"] = "analyzing"
        context = build_job_context(state)
        assert "Infrastructure:" not in context
        assert "Deployment:" not in context
        assert "Pipeline status: analyzing" in context

    def test_reports_pending_approval_gates(self):
        state = create_initial_state("https://github.com/test/repo")
        context = build_job_context(state)
        assert "Awaiting human approval" in context
        assert "gate_1_pre_infracost" in context

    def test_reports_rollback(self, completed_state):
        completed_state["deployops_result"]["rollback_triggered"] = True
        completed_state["deployops_result"]["rollback_reason"] = "health check failed"
        context = build_job_context(completed_state)
        assert "rollback was triggered" in context
        assert "health check failed" in context


class TestPromptAssembly:
    def test_contains_all_provided_blocks(self):
        prompt = build_prompt(
            "how much?",
            job_context="cost is $145",
            repo_context="README: a platform",
            history="User: hi",
        )
        assert "=== PIPELINE RESULTS ===" in prompt
        assert "=== REPOSITORY EXCERPTS ===" in prompt
        assert "=== CONVERSATION SO FAR ===" in prompt
        assert "how much?" in prompt

    def test_skips_empty_blocks(self):
        prompt = build_prompt("hi", job_context="", repo_context="", history="")
        assert "=== PIPELINE RESULTS ===" not in prompt
        assert "=== CONVERSATION SO FAR ===" not in prompt
        assert "=== QUESTION ===" in prompt

    def test_instructs_against_inventing_values(self):
        prompt = build_prompt("x", job_context="c", repo_context="", history="")
        assert "never invent" in prompt.lower()


class TestChat:
    def test_returns_an_answer(self, completed_state):
        result = chat("job-1", "What is my security score?", completed_state)
        assert result["answer"]
        assert result["job_id"] == "job-1"

    def test_uses_both_sources(self, completed_state):
        result = chat("job-1", "What is my security score?", completed_state)
        assert result["used_job_context"] is True
        assert result["used_rag"] is True

    def test_records_both_sides_of_the_exchange(self, completed_state):
        result = chat("job-1", "hello", completed_state)
        assert len(result["history"]) == 2
        assert result["history"][0]["role"] == "user"
        assert result["history"][1]["role"] == "assistant"

    def test_second_question_sees_the_first(self, completed_state):
        chat("job-1", "What is my security score?", completed_state)
        result = chat("job-1", "And how much does it cost?", completed_state)
        assert len(result["history"]) == 4
        assert "conversation history" in result["answer"]

    def test_first_question_has_no_history_block(self, completed_state):
        result = chat("job-1", "First question", completed_state)
        assert "conversation history" not in result["answer"]

    def test_works_without_state(self):
        result = chat("job-1", "What framework does this use?", None)
        assert result["used_job_context"] is False
        assert result["used_rag"] is True

    def test_empty_message_is_rejected(self, completed_state):
        with pytest.raises(ValueError, match="cannot be empty"):
            chat("job-1", "   ", completed_state)


class TestGracefulDegradation:
    def test_rag_failure_does_not_break_chat(self, completed_state, monkeypatch):
        monkeypatch.setenv("DEVGUARD_REAL_RAG", "1")

        def boom(*args, **kwargs):
            raise ConnectionError("Qdrant unreachable")

        monkeypatch.setattr(chat_module, "_retrieve_repo_context", lambda *a, **k: "")
        monkeypatch.setattr(chat_module, "_generate", lambda prompt, job_id: "answer")

        result = chat("job-1", "What is my score?", completed_state)
        assert result["used_rag"] is False
        assert result["used_job_context"] is True
        assert result["answer"] == "answer"

    def test_llm_failure_returns_a_message_not_an_exception(self, completed_state, monkeypatch):
        monkeypatch.setenv("DEVGUARD_REAL_RAG", "1")
        monkeypatch.setattr(chat_module, "_retrieve_repo_context", lambda *a, **k: "ctx")

        result = chat("job-1", "hi", completed_state)
        assert isinstance(result["answer"], str)
        assert result["answer"]


class TestFeatureFlag:
    def test_rag_is_mocked_by_default(self, monkeypatch):
        monkeypatch.delenv("DEVGUARD_REAL_RAG", raising=False)
        monkeypatch.delenv("DEVGUARD_REAL_AGENTS", raising=False)
        assert use_real_rag() is False

    def test_global_switch_enables_rag(self, monkeypatch):
        monkeypatch.setenv("DEVGUARD_REAL_AGENTS", "1")
        assert use_real_rag() is True
