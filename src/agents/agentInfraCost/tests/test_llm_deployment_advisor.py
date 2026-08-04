"""Tests for core.llm_deployment_advisor.

core.llm_provider.call_llm is always monkeypatched here — no test reaches
OpenRouter for real. The focus is the validation/fallback contract: the LLM
may only ever pick region/environment from the two closed lists, never an
arbitrary value, and any failure falls back to "us-east-1"/"dev".
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.llm_deployment_advisor import decide_deployment_context
from models.input_schema import RepoAnalysisInput

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_analysis(filename: str) -> RepoAnalysisInput:
    raw = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    return RepoAnalysisInput.model_validate(raw)


def _patch_call_llm(monkeypatch: pytest.MonkeyPatch, return_value: Any) -> None:
    monkeypatch.setattr(
        "core.llm_deployment_advisor.call_llm", lambda *args, **kwargs: return_value
    )


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_llm_choice_is_used_when_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(
        monkeypatch,
        json.dumps({"region": "eu-west-1", "environment": "prod", "reasoning": "Public visé en Europe."}),
    )

    context = decide_deployment_context(
        analysis, job_id="job-1", docker_image="devguard-app:job-1"
    )

    assert context.region == "eu-west-1"
    assert context.environment == "prod"
    assert context.job_id == "job-1"
    assert context.docker_image == "devguard-app:job-1"


def test_source_code_path_passes_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(monkeypatch, None)

    context = decide_deployment_context(
        analysis, job_id="job-1", docker_image=None, source_code_path="/tmp/repo.zip"
    )

    assert context.source_code_path == "/tmp/repo.zip"


# --------------------------------------------------------------------------
# Limit / edge cases -- every one of these must fall back to the defaults
# --------------------------------------------------------------------------


def test_falls_back_to_defaults_when_call_llm_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(monkeypatch, None)

    context = decide_deployment_context(analysis, job_id="job-1", docker_image=None)

    assert context.region == "us-east-1"
    assert context.environment == "dev"


def test_falls_back_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(monkeypatch, "not json at all")

    context = decide_deployment_context(analysis, job_id="job-1", docker_image=None)

    assert context.region == "us-east-1"
    assert context.environment == "dev"


def test_falls_back_when_region_outside_known_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the validation layer: the LLM cannot smuggle an unpriced
    region (no entry in data/aws_pricing.json's region_multipliers) in."""
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(
        monkeypatch,
        json.dumps({"region": "sa-east-1", "environment": "dev", "reasoning": "x"}),
    )

    context = decide_deployment_context(analysis, job_id="job-1", docker_image=None)

    assert context.region == "us-east-1"
    assert context.environment == "dev"


def test_falls_back_when_environment_outside_known_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(
        monkeypatch,
        json.dumps({"region": "us-east-1", "environment": "sandbox", "reasoning": "x"}),
    )

    context = decide_deployment_context(analysis, job_id="job-1", docker_image=None)

    assert context.region == "us-east-1"
    assert context.environment == "dev"


def test_falls_back_when_reasoning_field_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(
        monkeypatch, json.dumps({"region": "eu-west-1", "environment": "prod"})
    )

    context = decide_deployment_context(analysis, job_id="job-1", docker_image=None)

    assert context.region == "us-east-1"
    assert context.environment == "dev"


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_falls_back_when_response_is_a_json_array_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(monkeypatch, json.dumps(["us-east-1", "dev"]))

    context = decide_deployment_context(analysis, job_id="job-1", docker_image=None)

    assert context.region == "us-east-1"
    assert context.environment == "dev"
