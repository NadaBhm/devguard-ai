"""Tests for core.llm_terraform_refiner (Phase 6: Gate-2 feedback loop).

core.llm_provider.call_llm is always monkeypatched here — no test reaches
OpenRouter for real. The focus is the fail-soft contract:

  - valid LLM output -> the refined files replace the originals
  - a valid dockerfile in the reply refines the Dockerfile alongside them
  - every failure mode (None, malformed JSON, missing/empty fields) ->
    the ORIGINAL files come back untouched, never an error.
  - transient provider flakiness -> the refiner re-asks (a fresh request per
    attempt) and only gives up after REFINER_MAX_ATTEMPTS, so a single
    bad reply doesn't silently drop the user's regeneration request.

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


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries must not slow the unit tests: zero the backoff sleep."""
    monkeypatch.setattr("core.llm_terraform_refiner.REFINER_RETRY_DELAY_SECONDS", 0)


def _valid_reply(**overrides) -> str:
    payload = {
        "main_tf": CURRENT.main_tf,
        "variables_tf": CURRENT.variables_tf,
        "outputs_tf": CURRENT.outputs_tf,
    }
    payload.update(overrides)
    return json.dumps(payload)


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

    def test_repo_context_is_included_in_the_prompt(self, monkeypatch) -> None:
        """Gate-2 regeneration with a whole-repo digest: the LLM must see the
        repo facts, not just the rendered artifacts."""
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

        refine_terraform(
            CURRENT, "use two AZs", repo_context="app listens on port 9000, /healthz"
        )

        assert "=== CONTEXTE DU DÉPÔT ===" in captured["prompt"]
        assert "port 9000" in captured["prompt"]

    def test_repo_context_is_omitted_when_absent(self, monkeypatch) -> None:
        """Backward compatibility: no digest, no repo section in the prompt."""
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

        refine_terraform(CURRENT, "cheaper")

        assert "=== CONTEXTE DU DÉPÔT ===" not in captured["prompt"]

    def test_valid_dockerfile_in_reply_refines_it(self, monkeypatch) -> None:
        """A container run with a Dockerfile: the LLM may edit it too when the
        feedback explicitly targets the Dockerfile."""
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

        files, dockerfile = refine_terraform(
            CURRENT,
            "utilise python 3.11 dans le dockerfile et multi-stage",
            dockerfile=DOCKERFILE,
        )

        assert files == CURRENT
        assert dockerfile == refined_dockerfile

    def test_dockerfile_not_targeted_by_feedback_keeps_original(self, monkeypatch) -> None:
        """Feedback that doesn't mention container concerns must NOT let the
        LLM's rewritten Dockerfile through — the repo's real Dockerfile stays
        untouched even if the reply carries a (possibly broken) replacement."""
        llm_dockerfile = "FROM scratch\nCOPY . /app\nCMD [\"bogus\"]\n"
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": llm_dockerfile,
                }
            ),
        )

        files, dockerfile = refine_terraform(CURRENT, "make it cheaper", dockerfile=DOCKERFILE)

        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_dockerfile_kept_when_feedback_only_asks_terraform(self, monkeypatch) -> None:
        """The exact regression from the real run: 'make it cheaper' rewrote
        the Dockerfile into a broken generic one. It must now be preserved."""
        _patch_call_llm(
            monkeypatch,
            json.dumps(
                {
                    "main_tf": CURRENT.main_tf,
                    "variables_tf": CURRENT.variables_tf,
                    "outputs_tf": CURRENT.outputs_tf,
                    "dockerfile": (
                        "FROM python:3.12-slim\nCOPY requirements.txt .\nCMD uvicorn main:app\n"
                    ),
                }
            ),
        )

        files, dockerfile = refine_terraform(CURRENT, "make it cheaper", dockerfile=DOCKERFILE)

        assert files == CURRENT
        assert dockerfile == DOCKERFILE

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


class TestRefinerRetries:
    """The free OpenRouter tiers flake on the first reply; the refiner must
    re-ask (a fresh request each attempt) before settling for fail-soft."""

    def test_retries_after_unusable_output_then_applies(self, monkeypatch) -> None:
        calls = {"count": 0}

        def flaky_call_llm(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return None
            return _valid_reply(
                main_tf='resource "aws_ecs_cluster" "app" {\n  name = "retried"\n}'
            )

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", flaky_call_llm)

        files, dockerfile = refine_terraform(CURRENT, "retry please")

        assert calls["count"] == 2
        assert "retried" in files.main_tf
        assert dockerfile is None

    def test_retries_past_malformed_reply(self, monkeypatch) -> None:
        calls = {"count": 0}

        def flaky_call_llm(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] < 3:
                return "not json at all"
            return _valid_reply(outputs_tf='output "alb" {\n  value = "x"\n}')

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", flaky_call_llm)

        files, _ = refine_terraform(CURRENT, "keep trying")

        assert calls["count"] == 3
        assert '"x"' in files.outputs_tf

    def test_exhausts_attempts_then_keeps_originals(self, monkeypatch) -> None:
        calls = {"count": 0}

        def always_bad(*args, **kwargs):
            calls["count"] += 1
            return "not json"

        monkeypatch.setattr("core.llm_terraform_refiner.call_llm", always_bad)
        monkeypatch.setattr("core.llm_terraform_refiner.REFINER_MAX_ATTEMPTS", 3)

        files, dockerfile = refine_terraform(CURRENT, "x", dockerfile=DOCKERFILE)

        assert calls["count"] == 3
        assert files == CURRENT
        assert dockerfile == DOCKERFILE

    def test_markdown_fenced_json_is_parsed(self, monkeypatch) -> None:
        _patch_call_llm(
            monkeypatch,
            "```json\n"
            + _valid_reply(main_tf='resource "aws_ecs_cluster" "app" {\n  name = "fenced"\n}')
            + "\n```",
        )

        files, _ = refine_terraform(CURRENT, "fence me")

        assert "fenced" in files.main_tf
