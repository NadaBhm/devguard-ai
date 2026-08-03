"""Tests for core.llm_enrichment — fallback mode ONLY.

Per the mission: no real GEMINI_API_KEY, no network call, ever, in this
test file. Every test either unsets GEMINI_API_KEY (forcing the fallback
path deterministically) or, where an API key is simulated, monkeypatches
the Gemini client itself so nothing ever leaves the process.
"""

import pytest

from core.decision_engine import DecisionResult
from core.finops_optimizer import FinOpsRecommendation, OptimizationOption
from core.llm_enrichment import (
    _call_gemini,
    build_enrichment,
    explain_architecture_decision,
    explain_finops_choice,
    summarize_cost_estimation,
)
from models.output_schema import Money

_DECISION = DecisionResult(
    compute_type="ecs",
    sizing={"task_cpu": "512", "task_memory": "1024"},
    score_breakdown={"ecs": 7.0, "lambda": -5.0, "ec2": 2.0},
)
_COST = Money(amount=14.42, currency="USD", range_min=11.53, range_max=17.30)
_FINOPS = FinOpsRecommendation(
    recommended=OptimizationOption(name="spot", reason="Sûr ici, scaling horizontal détecté."),
    discarded=[OptimizationOption(name="graviton", reason="Déjà pris en compte dans le coût de base.")],
    context={"compose_detected": True, "horizontal_scaling_detected": True},
)


@pytest.fixture(autouse=True)
def _no_gemini_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs with no API key unless it opts in
    explicitly — the mission requires fallback-only testing here."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_explain_architecture_decision_falls_back_without_key() -> None:
    text, source = explain_architecture_decision(_DECISION)
    assert source == "fallback"
    assert "ecs" in text
    assert "7.0" in text


def test_summarize_cost_estimation_falls_back_without_key() -> None:
    text, source = summarize_cost_estimation(_DECISION, _COST)
    assert source == "fallback"
    assert "14.42" in text
    assert "USD" in text


def test_explain_finops_choice_falls_back_without_key() -> None:
    text, source = explain_finops_choice(_FINOPS)
    assert source == "fallback"
    assert "spot" in text
    assert "graviton" in text


def test_build_enrichment_assembles_all_three_as_fallback() -> None:
    enrichment = build_enrichment(_DECISION, _COST, _FINOPS)

    assert enrichment.enrichment_source == "fallback"
    assert "ecs" in enrichment.architecture_explanation
    assert "14.42" in enrichment.cost_summary
    assert "spot" in enrichment.finops_justification


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_call_gemini_returns_none_without_key() -> None:
    assert _call_gemini("prompt", "system") is None


def test_call_gemini_falls_back_on_any_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key IS present here, but the client itself is monkeypatched to
    raise — proving the try/except covers real failures too, never just
    the missing-key case. No real network call happens."""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-real-key")

    class _ExplodingClient:
        def __init__(self, api_key: str) -> None:
            raise RuntimeError("simulated Gemini outage")

    monkeypatch.setattr("shared.llm.gemini.gemini_client.GeminiClient", _ExplodingClient)

    assert _call_gemini("prompt", "system") is None


def test_enrichment_source_is_fallback_even_if_only_one_of_three_would_succeed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enrichment_source must never claim "gemini" unless ALL three texts
    really came from it — simulate one real success and two fallbacks."""
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-real-key")
    calls = {"n": 0}

    def _fake_call_gemini(prompt: str, system_instruction: str) -> str | None:
        calls["n"] += 1
        return "a real gemini answer" if calls["n"] == 1 else None

    monkeypatch.setattr("core.llm_enrichment._call_gemini", _fake_call_gemini)

    enrichment = build_enrichment(_DECISION, _COST, _FINOPS)

    assert enrichment.enrichment_source == "fallback"


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_malformed_score_breakdown_raises_not_silently_swallowed() -> None:
    """The try/except only shields the Gemini call itself — our own
    fallback-text formatting must still fail loudly on bad input."""
    broken_decision = DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": "512", "task_memory": "1024"},
        score_breakdown={"ecs": 7.0},  # lambda/ec2 missing on purpose
    )
    with pytest.raises(KeyError):
        explain_architecture_decision(broken_decision)


def test_enrichment_rejects_invalid_source() -> None:
    from pydantic import ValidationError

    from models.output_schema import Enrichment

    with pytest.raises(ValidationError):
        Enrichment(
            architecture_explanation="x",
            cost_summary="y",
            finops_justification="z",
            enrichment_source="made_up_source",  # type: ignore[arg-type]
        )
