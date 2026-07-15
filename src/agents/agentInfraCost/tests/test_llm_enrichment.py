"""Tests for llm_enrichment.py.

Per the module spec, these tests run in fallback mode only: no real
GEMINI_API_KEY and no network call is ever made. Where the Gemini branch
itself needs exercising (call failure, empty response, success), the
module's own `_call_gemini_sync` is monkeypatched directly rather than the
shared GeminiClient — this avoids depending on `google-generativeai` being
installed at all (it isn't, in this environment) while still covering
`_generate_text`'s full branching logic honestly.
"""

import pytest

import agentInfraCost.core.llm_enrichment as llm_enrichment
from agentInfraCost.core.llm_enrichment import (
    enrich,
    explain_architecture_decision,
    explain_finops_choice,
    summarize_cost_estimation,
)
from agentInfraCost.models.internal_models import (
    CostEstimateResult,
    DecisionResult,
    EcsSizing,
    FinOpsOption,
    FinOpsResult,
    ScoreBreakdown,
)


@pytest.fixture(autouse=True)
def _no_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file starts with no GEMINI_API_KEY set, matching
    the "must work with zero configuration" requirement. Individual tests
    override this via monkeypatch.setenv when they need to exercise the
    Gemini branch (still without a real key or network call)."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def _decision() -> DecisionResult:
    return DecisionResult(
        compute_type="ecs",
        ecs=EcsSizing(
            cluster="devguard-cluster",
            service_name="repo-name-service",
            task_cpu=512,
            task_memory=1024,
            health_check_path="/health",
            health_check_port=8080,
            timeout_minutes=5,
            min_healthy_percent=50,
            max_percent=200,
        ),
        score_breakdown=ScoreBreakdown(
            ecs_score=8.0, lambda_score=0.0, ec2_score=2.5, signals={"container_detected": True}
        ),
        reasoning=["container_detected=True", "selected compute_type='ecs'"],
    )


def _cost() -> CostEstimateResult:
    return CostEstimateResult(amount=18.02, currency="USD", range_min=14.42, range_max=21.62)


def _finops() -> FinOpsResult:
    return FinOpsResult(
        recommended_option="reserved",
        discarded_options=[
            FinOpsOption(name="spot", recommended=False, reason="too risky here"),
            FinOpsOption(name="graviton", recommended=False, reason="reserved already chosen"),
            FinOpsOption(name="on_demand", recommended=False, reason="undiscounted baseline"),
        ],
        context_used={"critical_stateful": True, "compute_type": "ecs"},
    )


class TestFallbackModeNoApiKey:
    def test_explain_architecture_decision_falls_back(self) -> None:
        text, source = explain_architecture_decision(_decision())
        assert source == "fallback"
        assert "ECS" in text
        assert isinstance(text, str) and len(text) > 0

    def test_summarize_cost_estimation_falls_back(self) -> None:
        text, source = summarize_cost_estimation(_decision(), _cost())
        assert source == "fallback"
        assert "18.02" in text
        assert "USD" in text

    def test_explain_finops_choice_falls_back(self) -> None:
        text, source = explain_finops_choice(_finops())
        assert source == "fallback"
        assert "reserved" in text

    def test_enrich_produces_full_block_with_fallback_source(self) -> None:
        result = enrich(decision=_decision(), cost=_cost(), finops=_finops())
        assert result.enrichment_source == "fallback"
        assert "ECS" in result.architecture_explanation
        assert "18.02" in result.cost_summary
        assert "reserved" in result.finops_justification

    def test_enrich_never_raises_and_never_needs_network(self) -> None:
        """Nominal, zero-configuration path required by the pipeline spec:
        must work end to end with no GEMINI_API_KEY, no exception."""
        result = enrich(decision=_decision(), cost=_cost(), finops=_finops())
        assert result.enrichment_source in ("gemini", "fallback")


class TestGeminiBranchViaMonkeypatchedCall:
    """Exercises _generate_text's Gemini branch without touching the real
    GeminiClient or making any network call."""

    def test_successful_gemini_call_is_used_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing-only")
        monkeypatch.setattr(
            llm_enrichment, "_call_gemini_sync", lambda prompt, *, timeout_seconds: "  Gemini text.  "
        )
        text, source = explain_architecture_decision(_decision())
        assert source == "gemini"
        assert text == "Gemini text."

    def test_gemini_failure_falls_back_without_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing-only")

        def _raise(*args, **kwargs):
            raise TimeoutError("simulated network timeout")

        monkeypatch.setattr(llm_enrichment, "_call_gemini_sync", _raise)
        text, source = explain_architecture_decision(_decision())
        assert source == "fallback"
        assert "ECS" in text

    def test_gemini_empty_response_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing-only")
        monkeypatch.setattr(
            llm_enrichment, "_call_gemini_sync", lambda prompt, *, timeout_seconds: "   "
        )
        text, source = explain_architecture_decision(_decision())
        assert source == "fallback"

    def test_enrich_is_only_gemini_source_when_all_three_succeed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing-only")
        monkeypatch.setattr(
            llm_enrichment, "_call_gemini_sync", lambda prompt, *, timeout_seconds: "ok"
        )
        result = enrich(decision=_decision(), cost=_cost(), finops=_finops())
        assert result.enrichment_source == "gemini"

    def test_enrich_is_fallback_source_when_only_one_of_three_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing-only")
        calls = {"n": 0}

        def _flaky(prompt, *, timeout_seconds):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated failure on the second call")
            return "ok"

        monkeypatch.setattr(llm_enrichment, "_call_gemini_sync", _flaky)
        result = enrich(decision=_decision(), cost=_cost(), finops=_finops())
        assert result.enrichment_source == "fallback"


class TestMissingOptionalDependencyIsHandledGracefully:
    def test_real_call_path_raises_import_error_which_is_caught(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With a (fake) key set and _call_gemini_sync NOT monkeypatched,
        the real lazy import of google-generativeai is attempted. In this
        environment that dependency isn't installed, so it raises
        ImportError — which must be caught by _generate_text's broad
        except, not propagate."""
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing-only")
        text, source = explain_architecture_decision(_decision(), timeout_seconds=1.0)
        assert source == "fallback"
        assert "ECS" in text
