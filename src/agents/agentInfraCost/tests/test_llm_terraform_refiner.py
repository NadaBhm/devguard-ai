"""Tests for core.llm_terraform_refiner (Phase 6: Gate-2 feedback loop).

core.llm_provider.call_llm is always monkeypatched here — no test reaches
OpenRouter for real. The focus is the fail-soft contract:

  - valid LLM output -> the refined files replace the originals
  - every failure mode (None, malformed JSON, missing/empty fields) ->
    the ORIGINAL files come back untouched, never an error.
"""

from __future__ import annotations

import json

import pytest

from core.llm_terraform_refiner import refine_terraform
from models.output_schema import TerraformFiles

CURRENT = TerraformFiles(
    main_tf='resource "aws_ecs_cluster" "app" {\n  name = "devguard"\n}',
    variables_tf='variable "aws_region" {\n  default = "us-east-1"\n}',
    outputs_tf='output "alb_dns" {\n  value = aws_lb.app.dns_name\n}',
)


def _patch_call_llm(monkeypatch: pytest.MonkeyPatch, return_value) -> None:
    monkeypatch.setattr(
        "core.llm_terraform_refiner.call_llm",
        lambda *args, **kwargs: return_value,
    )


class TestRefinerNominal:
    def test_valid_output_replaces_files(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": 'resource "aws_ecs_cluster" "app" {\n  name = "devguard-cost-optimized"\n}',
                    "variables_tf": 'variable "aws_region" {\n  default = "eu-west-1"\n}',
                    "outputs_tf": 'output "alb_dns" {\n  value = aws_lb.app.dns_name\n}',
                }
            ),
        )

        result = refine_terraform(CURRENT, "use eu-west-1 and a cheaper cluster")

        assert result.main_tf != CURRENT.main_tf
        assert "eu-west-1" in result.variables_tf
        assert isinstance(result, TerraformFiles)

    def test_feedback_is_included_in_the_prompt(self, monkeypatch) -> None:
        captured = {}

        def fake_call_llm(*args, **kwargs):
            captured["prompt"] = kwargs.get("prompt", args[0] if args else None)
            return json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                }
            )

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", fake_call_llm)

        refine_terraform(CURRENT, "make it cheaper please")

        assert "make it cheaper please" in captured["prompt"]


class TestRefinerFailSoft:
    """Every failure path must return the originals unchanged."""

    def test_none_reply_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, None)
        assert refine_terraform(CURRENT, "x") == CURRENT

    def test_malformed_json_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, "this is not json")
        assert refine_terraform(CURRENT, "x") == CURRENT

    def test_missing_fields_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, json.dumps({"main_tf": "only this"}))
        assert refine_terraform(CURRENT, "x") == CURRENT

    def test_empty_file_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": "",
                    "variables_tf": "   ",
                    "outputs_tf": 'output "x" {}\n',
                }
            ),
        )
        assert refine_terraform(CURRENT, "x") == CURRENT

    def test_empty_string_reply_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, "")
        assert refine_terraform(CURRENT, "x") == CURRENT
