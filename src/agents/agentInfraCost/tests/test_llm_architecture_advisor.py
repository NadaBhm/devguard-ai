"""Tests for core.llm_architecture_advisor.

core.llm_provider.call_llm is always monkeypatched here — no test reaches
OpenRouter for real. The focus of this file is the validation/fallback
contract: the LLM may only ever select one of the three known compute
types, never invent sizing, and any failure mode falls back to
decide_architecture()'s deterministic result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.decision_engine import decide_architecture
from core.llm_architecture_advisor import decide_architecture_via_llm
from models.input_schema import RepoAnalysisInput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_analysis(filename: str) -> RepoAnalysisInput:
    raw = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    return RepoAnalysisInput.model_validate(raw)


def _patch_call_llm(monkeypatch: pytest.MonkeyPatch, return_value: Any) -> None:
    if isinstance(return_value, Exception):
        def _raise(*args, **kwargs):
            raise return_value

        monkeypatch.setattr("core.llm_architecture_advisor.call_llm", _raise)
    else:
        monkeypatch.setattr(
            "core.llm_architecture_advisor.call_llm", lambda *args, **kwargs: return_value
        )


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_llm_choice_is_used_when_valid_and_differs_from_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = _load_analysis("sample_input.json")
    deterministic = decide_architecture(analysis)
    other_type = "lambda" if deterministic.compute_type != "lambda" else "ec2"

    _patch_call_llm(
        monkeypatch,
        json.dumps({"compute_type": other_type, "reasoning": "Raison test du LLM."}),
    )

    result = decide_architecture_via_llm(analysis)

    assert result.compute_type == other_type
    assert result.decision_source == "llm"
    assert result.llm_reasoning == "Raison test du LLM."
    # score_breakdown stays the deterministic one, kept as context only
    assert result.score_breakdown == deterministic.score_breakdown


def test_llm_choice_reuses_deterministic_sizing_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The LLM never invents sizing -- compute_sizing() must produce the
    exact same result decide_architecture() would for that compute_type."""
    analysis = _load_analysis("sample_input.json")

    _patch_call_llm(
        monkeypatch, json.dumps({"compute_type": "lambda", "reasoning": "Petit projet."})
    )

    result = decide_architecture_via_llm(analysis)

    assert set(result.sizing.keys()) == {"memory_mb"}


# --------------------------------------------------------------------------
# Limit / edge cases -- every one of these must fall back deterministically
# --------------------------------------------------------------------------


def test_falls_back_when_call_llm_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis = _load_analysis("sample_input.json")
    deterministic = decide_architecture(analysis)
    _patch_call_llm(monkeypatch, None)

    result = decide_architecture_via_llm(analysis)

    assert result.decision_source == "deterministic"
    assert result.compute_type == deterministic.compute_type
    assert result.llm_reasoning is None


def test_falls_back_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis = _load_analysis("sample_input.json")
    deterministic = decide_architecture(analysis)
    _patch_call_llm(monkeypatch, "this is not json at all")

    result = decide_architecture_via_llm(analysis)

    assert result.decision_source == "deterministic"
    assert result.compute_type == deterministic.compute_type


def test_falls_back_when_compute_type_is_outside_known_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves the validation layer: the LLM cannot smuggle an arbitrary
    architecture (e.g. 'kubernetes') into the pipeline."""
    analysis = _load_analysis("sample_input.json")
    deterministic = decide_architecture(analysis)
    _patch_call_llm(
        monkeypatch,
        json.dumps({"compute_type": "kubernetes", "reasoning": "Not a supported value."}),
    )

    result = decide_architecture_via_llm(analysis)

    assert result.decision_source == "deterministic"
    assert result.compute_type == deterministic.compute_type


def test_falls_back_when_reasoning_field_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis = _load_analysis("sample_input.json")
    deterministic = decide_architecture(analysis)
    _patch_call_llm(monkeypatch, json.dumps({"compute_type": "ecs"}))

    result = decide_architecture_via_llm(analysis)

    assert result.decision_source == "deterministic"
    assert result.compute_type == deterministic.compute_type


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_falls_back_when_response_is_a_json_array_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = _load_analysis("sample_input.json")
    deterministic = decide_architecture(analysis)
    _patch_call_llm(monkeypatch, json.dumps(["ecs", "reasoning"]))

    result = decide_architecture_via_llm(analysis)

    assert result.decision_source == "deterministic"
    assert result.compute_type == deterministic.compute_type
