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


@pytest.fixture(autouse=True)
def _no_pinned_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full-suite run loads .env (src.backend.config.load_dotenv), which
    sets DEVGUARD_AWS_REGION=us-east-1 and makes decide_deployment_context
    skip call_llm entirely. Clear it so the LLM path is exercised, except in
    test_pinned_region_overrides_llm_choice which sets it explicitly."""
    monkeypatch.delenv("DEVGUARD_AWS_REGION", raising=False)


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


def test_llm_choice_is_used_when_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEVGUARD_AWS_REGION", raising=False)
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(
        monkeypatch,
        json.dumps(
            {"region": "eu-west-1", "environment": "prod", "reasoning": "Public visé en Europe."}
        ),
    )

    context = decide_deployment_context(
        analysis, job_id="job-1", docker_image="devguard-app:job-1"
    )

    assert context.region == "eu-west-1"
    assert context.environment == "prod"
    assert context.job_id == "job-1"
    assert context.docker_image == "devguard-app:job-1"


def test_database_is_passed_through_from_analysis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEVGUARD_AWS_REGION", raising=False)
    analysis = _load_analysis("sample_input.json")  # stack_detection.database == "postgresql"
    _patch_call_llm(monkeypatch, None)  # database isn't LLM-decided; irrelevant here

    context = decide_deployment_context(analysis, job_id="job-1", docker_image=None)

    assert context.database == "postgresql"


def test_pinned_region_overrides_llm_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEVGUARD_AWS_REGION is a deterministic kill-switch: the real AWS
    account lives in one region, so an LLM guessing another (e.g. eu-west-1
    while the VPC/ECR/RDS all sit in us-east-1) must never override it."""
    monkeypatch.setenv("DEVGUARD_AWS_REGION", "us-east-1")
    analysis = _load_analysis("sample_input.json")
    _patch_call_llm(
        monkeypatch,
        json.dumps({"region": "eu-west-1", "environment": "prod", "reasoning": "Europe"}),
    )

    context = decide_deployment_context(analysis, job_id="job-1", docker_image=None)

    assert context.region == "us-east-1"
    assert context.environment == "dev"  # environment is still LLM-decided


def test_repo_context_is_included_in_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gate-2 regeneration: the deployment-context LLM must see the whole-repo
    digest before picking region/environment."""
    analysis = _load_analysis("sample_input.json")
    analysis = analysis.model_copy(update={"repo_context": "EU users, prod traffic"})
    captured: dict = {}

    def _fake_call_llm(*args, **kwargs):
        captured["prompt"] = kwargs.get("prompt", args[0] if args else None)
        return json.dumps(
            {"region": "eu-west-1", "environment": "prod", "reasoning": "repo facts"}
        )

    monkeypatch.setattr("core.llm_deployment_advisor.call_llm", _fake_call_llm)

    decide_deployment_context(analysis, job_id="job-1", docker_image=None)

    assert "=== CONTEXTE DU DÉPÔT" in captured["prompt"]
    assert "EU users" in captured["prompt"]


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
