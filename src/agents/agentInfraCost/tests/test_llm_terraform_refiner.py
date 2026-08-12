"""Tests for core.llm_terraform_refiner (Phase 6: Gate-2 feedback loop).

core.llm_provider.call_llm is always monkeypatched here — no test reaches
OpenRouter for real. The focus is the fail-soft contract:

  - valid LLM output -> the refined files replace the originals
  - a valid dockerfile in the reply refines the Dockerfile alongside them
  - every failure mode (None, malformed JSON, missing/empty fields) ->
    the ORIGINAL files come back untouched, never an error.

refine_terraform now returns a ``(TerraformFiles, dockerfile)`` tuple; the
dockerfile is ``None`` when no current Dockerfile was supplied.
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

DOCKERFILE = "FROM python:3.12-slim\nCOPY . /app\n"


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

        files, dockerfile = refine_terraform(CURRENT, "use eu-west-1 and a cheaper cluster")

        assert files.main_tf != CURRENT.main_tf
        assert "eu-west-1" in files.variables_tf
        assert isinstance(files, TerraformFiles)
        # no dockerfile supplied -> returns None, never a fabricated file
        assert dockerfile is None

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

    def test_valid_dockerfile_in_reply_refines_it(self, monkeypatch) -> None:
        """A container run with a Dockerfile: the LLM may edit it too."""
        refined_dockerfile = "FROM python:3.11-slim\nRUN pip install --no-cache-dir -r requirements.txt\nCOPY . /app\n"
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": refined_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(CURRENT, "base python 3.11 et multi-stage", dockerfile=DOCKERFILE)

        assert files == CURRENT
        assert dockerfile == refined_dockerfile

    def test_dockerfile_absent_in_reply_keeps_original(self, monkeypatch) -> None:
        """Backward compatibility: a Terraform-only reply must not wipe the
        current Dockerfile."""
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                }
            ),
        )

        _, dockerfile = refine_terraform(CURRENT, "cheaper please", dockerfile=DOCKERFILE)

        assert dockerfile == DOCKERFILE

    def test_empty_dockerfile_in_reply_keeps_original(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": "   ",
                }
            ),
        )

        _, dockerfile = refine_terraform(CURRENT, "cheaper please", dockerfile=DOCKERFILE)

        assert dockerfile == DOCKERFILE


class TestRefinerFailSoft:
    """Every failure path must return the originals unchanged."""

    def test_none_reply_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, None)
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_malformed_json_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, "this is not json")
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_missing_fields_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, json.dumps({"main_tf": "only this"}))
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

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
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_empty_string_reply_returns_originals(self, monkeypatch) -> None:
        _patch_call_llm(monkeypatch, "")
        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)
        assert files == CURRENT
        assert dockerfile == DOCKERFILE
