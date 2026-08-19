"""Tests for core.pipeline."""

import json
from pathlib import Path

import pytest
from core.decision_engine import DecisionResult
from core.input_validator import LowConfidenceError
from core.pipeline import (
    PipelineStageError,
    _recompute_decision_from_refined,
    _required_env_vars,
    _sizing_from_refined_terraform,
    run_pipeline,
    run_pipeline_with_context,
)
from models.output_schema import (
    Ec2InfraCostOutput,
    EcsInfraCostOutput,
    LambdaInfraCostOutput,
    TerraformFiles,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_raw(filename: str) -> dict:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Nominal cases
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected_type",
    [
        ("sample_input.json", EcsInfraCostOutput),
        ("sample_input_variant_lambda_candidate.json", LambdaInfraCostOutput),
        ("sample_input_variant_node_ecs.json", EcsInfraCostOutput),
    ],
)
def test_run_pipeline_end_to_end(filename: str, expected_type: type) -> None:
    output = run_pipeline(_load_raw(filename))

    assert isinstance(output, expected_type)


def test_run_pipeline_with_context_returns_same_output_as_run_pipeline() -> None:
    """The two entry points must agree — run_pipeline_with_context is a
    richer view into the same pipeline, never a second implementation."""
    raw = _load_raw("sample_input.json")

    plain_output = run_pipeline(raw)
    context = run_pipeline_with_context(raw)

    assert context.output == plain_output
    assert context.decision.compute_type == plain_output.compute_type


def test_run_pipeline_with_context_exposes_decision_and_finops() -> None:
    context = run_pipeline_with_context(_load_raw("sample_input.json"))

    assert context.decision.compute_type == "ecs"
    assert context.finops.recommended is not None
    assert context.output.aws_config.estimated_monthly_cost.amount > 0
    assert context.output.enrichment.enrichment_source == "fallback"  # no GEMINI_API_KEY in tests


def test_run_pipeline_ecs_has_real_terraform() -> None:
    output = run_pipeline(_load_raw("sample_input.json"))
    assert "aws_ecs_cluster" in output.artifacts.terraform.files.main_tf


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_low_confidence_propagates_unwrapped_not_as_pipeline_stage_error() -> None:
    """Module 1's own typed exceptions must reach the caller directly —
    wrapping them in PipelineStageError would hide their .job_id."""
    with pytest.raises(LowConfidenceError) as excinfo:
        run_pipeline(_load_raw("sample_input_variant_low_confidence.json"))
    assert excinfo.value.job_id == "job-variant-003"
    assert not isinstance(excinfo.value, PipelineStageError)


def test_pipeline_stage_error_names_decision_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(analysis):
        raise ValueError("simulated decision_engine crash")

    monkeypatch.setattr("core.pipeline.decide_architecture_via_llm", _boom)

    with pytest.raises(PipelineStageError) as excinfo:
        run_pipeline(_load_raw("sample_input.json"))

    assert excinfo.value.stage == "decision_engine"
    assert isinstance(excinfo.value.original_exception, ValueError)


def test_pipeline_stage_error_names_cost_estimator(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(decision, context=None):
        raise RuntimeError("simulated cost_estimator crash")

    monkeypatch.setattr("core.pipeline.estimate_cost", _boom)

    with pytest.raises(PipelineStageError) as excinfo:
        run_pipeline(_load_raw("sample_input.json"))

    assert excinfo.value.stage == "cost_estimator"


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_pipeline_stage_error_names_output_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated output_builder crash")

    monkeypatch.setattr("core.pipeline.build_output", _boom)

    with pytest.raises(PipelineStageError) as excinfo:
        run_pipeline(_load_raw("sample_input.json"))

    assert excinfo.value.stage == "output_builder"
    assert "simulated output_builder crash" in str(excinfo.value)


def test_ec2_synthetic_large_project_end_to_end() -> None:
    """No fixture naturally picks ec2 — build one from a large, container-less project."""
    raw = _load_raw("sample_input_variant_lambda_candidate.json")
    raw["job_id"] = "job-ec2-pipeline-test"
    raw["repo_metadata"]["loc"] = 50_000
    raw["repo_metadata"]["total_files"] = 500

    output = run_pipeline(raw)

    assert isinstance(output, Ec2InfraCostOutput)


# --------------------------------------------------------------------------
# Gate-2 regeneration: whole-repo context
# --------------------------------------------------------------------------


def test_gate2_repo_digest_is_computed_and_reaches_architecture_advisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When Gate 2 requests regeneration, the pipeline digests the re-cloned
    repo and feeds the result into the OpenRouter architecture prompt."""
    raw = _load_raw("sample_input.json")
    raw["user_feedback"] = "make it cheaper"
    raw["repo_path"] = str(tmp_path)

    captured: dict = {}

    def _fake_ingest(repo_path, job_id, *, commit_sha=None):
        captured["path"] = str(repo_path)
        return "port 8000, health check /health, FastAPI + Postgres"

    def _fake_arch_llm(*args, **kwargs):
        captured["prompt"] = kwargs.get("prompt", args[0] if args else None)
        return json.dumps({"compute_type": "ecs", "reasoning": "repo facts say so"})

    monkeypatch.setattr("core.pipeline.ingest_repo", _fake_ingest)
    monkeypatch.setattr("core.llm_architecture_advisor.call_llm", _fake_arch_llm)

    run_pipeline(raw)

    assert captured["path"] == str(tmp_path)
    assert "=== CONTEXTE DU DÉPÔT" in captured["prompt"]
    assert "port 8000" in captured["prompt"]


def test_gate2_without_repo_path_never_digests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No re-cloned repo path, no digest call — regression runs untouched."""
    raw = _load_raw("sample_input.json")
    raw["user_feedback"] = "cheaper please"

    called = {"n": 0}

    def _fake_ingest(*args, **kwargs):
        called["n"] += 1
        return "never"

    monkeypatch.setattr("core.pipeline.ingest_repo", _fake_ingest)

    run_pipeline(raw)

    assert called["n"] == 0


def test_first_try_with_repo_path_digests_and_refines_dockerfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM artifact pass now runs on the FIRST try too (not just Gate-2
    feedback): with a repo_path present the pipeline digests the repo and
    drives the refiner with the first-try instruction that forces a real
    Dockerfile — so a first deployment doesn't ship the bare stub Dockerfile
    and hardcoded 8080/"/health" that can't run the app."""
    raw = _load_raw("sample_input.json")
    raw["repo_path"] = "/tmp/some-cloned-repo"

    captured: dict = {}

    def _fake_ingest(repo_path, job_id, *, commit_sha=None):
        captured["digested"] = True
        return "Node/Express app listening on 3000 with /api/health"

    def _fake_arch_llm(*args, **kwargs):
        return json.dumps({"compute_type": "ecs", "reasoning": "repo facts say so"})

    def _fake_refiner(terraform_files, feedback, *, dockerfile=None, repo_context=None, force_dockerfile=False):
        captured["feedback"] = feedback
        captured["force_dockerfile"] = force_dockerfile
        return terraform_files, dockerfile

    monkeypatch.setattr("core.pipeline.ingest_repo", _fake_ingest)
    monkeypatch.setattr("core.llm_architecture_advisor.call_llm", _fake_arch_llm)
    monkeypatch.setattr("core.pipeline.refine_terraform", _fake_refiner)

    run_pipeline(raw)

    assert captured["digested"] is True
    assert captured["force_dockerfile"] is True
    assert "runnable Dockerfile" in captured["feedback"]
    assert captured["feedback"] != raw.get("user_feedback")


def test_first_try_with_real_dockerfile_does_not_force_regeneration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CodeSec captured a real Dockerfile, the first-try refiner must NOT
    regenerate it from the repo digest (force_dockerfile=False) — only apply
    the port/health correction. Regression for the Jupyter REST API E2E
    failure: the LLM rewrote a valid Dockerfile and spliced a stray
    ``RUN apk add`` into an apt-get block, so the image build died."""
    raw = _load_raw("sample_input.json")
    raw["repo_path"] = "/tmp/some-cloned-repo"
    raw["dockerfile_contents"] = {
        "Dockerfile": "FROM python:3.6-slim\nCMD uvicorn app:app --port ${PORT}\n"
    }

    captured: dict = {}

    def _fake_ingest(repo_path, job_id, *, commit_sha=None):
        return "FastAPI app on 8888"

    def _fake_arch_llm(*args, **kwargs):
        return json.dumps({"compute_type": "ecs", "reasoning": "facts"})

    def _fake_refiner(terraform_files, feedback, *, dockerfile=None, repo_context=None, force_dockerfile=False):
        captured["feedback"] = feedback
        captured["force_dockerfile"] = force_dockerfile
        captured["dockerfile"] = dockerfile
        return terraform_files, dockerfile

    monkeypatch.setattr("core.pipeline.ingest_repo", _fake_ingest)
    monkeypatch.setattr("core.llm_architecture_advisor.call_llm", _fake_arch_llm)
    monkeypatch.setattr("core.pipeline.refine_terraform", _fake_refiner)

    run_pipeline(raw)

    assert captured["force_dockerfile"] is False
    assert "runnable Dockerfile" not in captured["feedback"]
    # the real captured content is what gets passed to the refiner, untouched
    assert "python:3.6-slim" in captured["dockerfile"]


def test_first_try_without_repo_path_skips_refiner() -> None:
    """No repo_path, no first-try refinement — the deterministic artifacts
    ship unchanged (fail-soft, backward compatible)."""
    raw = _load_raw("sample_input.json")
    output = run_pipeline(raw)

    assert isinstance(output, EcsInfraCostOutput)
    assert output.artifacts.dockerfile is not None
    assert "COPY . /app" in output.artifacts.dockerfile


def test_health_path_inferred_to_root_when_app_has_no_health_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Apps without a /health endpoint must not get the template's hardcoded
    /health (which 404s -> rollback). A plain Next.js Dockerfile (npm run dev,
    no health route in the repo) should be probed at "/" instead."""
    raw = _load_raw("sample_input.json")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Dockerfile").write_text(
        "FROM node:18-alpine\nWORKDIR /app\nCOPY . .\nEXPOSE 3000\n"
        'CMD ["npm", "run", "dev"]\n'
    )
    raw["repo_path"] = str(repo)

    # Keep the refiner from touching the files; the deterministic inference
    # is what we're testing.
    monkeypatch.setattr("core.pipeline.ingest_repo", lambda *a, **k: "Next.js app on 3000")
    monkeypatch.setattr(
        "core.llm_architecture_advisor.call_llm",
        lambda *a, **k: json.dumps({"compute_type": "ecs", "reasoning": "test"}),
    )
    monkeypatch.setattr(
        "core.pipeline.refine_terraform",
        lambda tf, feedback, **k: (tf, None),
    )

    output = run_pipeline(raw)
    main = output.artifacts.terraform.files.main_tf
    assert 'path                = "/"' in main
    assert 'path                = "/health"' not in main


def test_health_path_kept_when_app_exposes_health_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the repo has an explicit health route, that path is kept (not
    overridden by the root fallback)."""
    raw = _load_raw("sample_input.json")
    repo = tmp_path / "repo"
    (repo / "src" / "app" / "health").mkdir(parents=True)
    (repo / "src" / "app" / "health" / "route.ts").write_text(
        'export async function GET() { return new Response("ok"); }\n'
    )
    (repo / "Dockerfile").write_text(
        "FROM node:18-alpine\nWORKDIR /app\nCOPY . .\nEXPOSE 3000\n"
        'CMD ["npm", "run", "dev"]\n'
    )
    raw["repo_path"] = str(repo)

    monkeypatch.setattr("core.pipeline.ingest_repo", lambda *a, **k: "Next.js app with health")
    monkeypatch.setattr(
        "core.llm_architecture_advisor.call_llm",
        lambda *a, **k: json.dumps({"compute_type": "ecs", "reasoning": "test"}),
    )
    monkeypatch.setattr(
        "core.pipeline.refine_terraform",
        lambda tf, feedback, dockerfile=None, **k: (tf, dockerfile),
    )

    output = run_pipeline(raw)
    main = output.artifacts.terraform.files.main_tf
    assert 'path                = "/health"' in main


def test_required_env_vars_detected_from_boot_gate() -> None:
    """A Dockerfile that hard-exits unless secrets are set surfaces those vars,
    minus conventional ones and vars it defines itself via ENV."""
    dockerfile = (
        "FROM node:18-alpine\n"
        'ENV NODE_ENV=production\n'
        'CMD sh -c "if [ -z \\"$MONGODB_URI\\" ] || [ -z \\"$JWT_TOKEN\\" ]; '
        'then echo missing; exit 1; fi; npm start"\n'
    )
    assert _required_env_vars(dockerfile) == ["JWT_TOKEN", "MONGODB_URI"]


def test_required_env_vars_ignore_defined_and_conventional() -> None:
    dockerfile = (
        "FROM node:18-alpine\n"
        'ENV PORT=3000\n'
        'CMD ["sh", "-c", "node server.js $PORT"]\n'
    )
    assert _required_env_vars(dockerfile) == []


def test_required_env_vars_none_when_no_dockerfile() -> None:
    assert _required_env_vars(None) == []


def test_required_env_vars_across_multiline_cmd_continuation() -> None:
    """Backslash-continued CMD blocks must keep collecting env refs past the
    line that opened the block (the Animetrix pattern)."""
    dockerfile = (
        "FROM node:18-alpine\n"
        'CMD sh -c "\\\n'
        'if [ -z \\"$MONGODB_URI\\" ] || \\\n'
        '   [ -z \\"$JWT_TOKEN\\" ]; then \\\n'
        '  echo missing; exit 1; \\\n'
        'fi; \\\n'
        ' npm run dev"\n'
    )
    assert _required_env_vars(dockerfile) == ["JWT_TOKEN", "MONGODB_URI"]


def test_multi_container_pipeline_qualifies_each_image_with_ecr() -> None:
    """Multi-container E2E: plural containers resolve to plural docker_images,
    each rendered as its own container_definition with an ECR-qualified URI.
    No repo_path -> refiner skipped, so this exercises the full deterministic
    path with account_id present (ECR qualification)."""
    raw = _load_raw("sample_input.json")
    raw["account_id"] = "111122223333"
    raw["stack_detection"]["containers"] = [
        {"detected": True, "base_image": "python:3.12-slim",
         "dockerfile_path": "backend/Dockerfile",
         "dockerfile_content": "FROM python:3.12-slim\nEXPOSE 8000\n",
         "compose_detected": False},
        {"detected": True, "base_image": "nginx:1.27",
         "dockerfile_path": "frontend/Dockerfile",
         "dockerfile_content": "FROM nginx:1.27\nEXPOSE 80\n",
         "compose_detected": False},
    ]
    raw["stack_detection"]["container"] = raw["stack_detection"]["containers"][0]

    output = run_pipeline(raw)

    assert isinstance(output, EcsInfraCostOutput)
    images = output.artifacts.docker_images
    assert len(images) == 2
    assert images[0].name == "devguard-app"
    assert images[1].name == "devguard-app-frontend"
    assert images[0].context == "backend"
    assert images[1].context == "frontend"
    assert images[0].dockerfile == "FROM python:3.12-slim\nEXPOSE 8000\n"
    # The primary's EXPOSE port is wired into the rendered Terraform.
    assert "containerPort = 8000" in output.artifacts.terraform.files.main_tf
    assert "containerPort = 80" in output.artifacts.terraform.files.main_tf
    # Singular alias mirrors the primary for legacy consumers.
    assert output.artifacts.docker_image.name == "devguard-app"
    assert "COPY . /app" not in output.artifacts.dockerfile
    assert "EXPOSE 8000" in output.artifacts.dockerfile


# --------------------------------------------------------------------------
# Cost follows the refiner's actual sizing (option 1)
# --------------------------------------------------------------------------


class TestSizingFromRefinedTerraform:
    def test_ecs_parses_cpu_and_memory(self) -> None:
        main_tf = (
            'resource "aws_ecs_task_definition" "this" {\n'
            '  cpu   = "512"\n'
            '  memory = "1024"\n'
            '}\n'
        )
        assert _sizing_from_refined_terraform(main_tf, "", "ecs") == {
            "task_cpu": 512,
            "task_memory": 1024,
        }

    def test_ecs_resolves_var_references_from_variables_tf(self) -> None:
        """The refiner renders sizing as `cpu = var.task_cpu` with the real
        value as a `default` in variables.tf — the cost must follow it."""
        main_tf = (
            'resource "aws_ecs_task_definition" "this" {\n'
            "  cpu   = var.task_cpu\n"
            "  memory = var.task_memory\n"
            "}\n"
        )
        variables_tf = (
            'variable "task_cpu" {\n  type = string\n  default = "4096"\n}\n'
            'variable "task_memory" {\n  type = string\n  default = "8192"\n}\n'
        )
        assert _sizing_from_refined_terraform(main_tf, variables_tf, "ecs") == {
            "task_cpu": 4096,
            "task_memory": 8192,
        }

    def test_ecs_parses_desired_count(self) -> None:
        main_tf = (
            'resource "aws_ecs_task_definition" "t" {\n'
            '  cpu   = "1024"\n'
            '  memory = "2048"\n'
            "}\n"
            'resource "aws_ecs_service" "s" {\n'
            "  desired_count = 3\n"
            "}\n"
        )
        assert _sizing_from_refined_terraform(main_tf, "", "ecs") == {
            "task_cpu": 1024,
            "task_memory": 2048,
            "desired_count": 3,
        }

    def test_ec2_parses_instance_type(self) -> None:
        main_tf = 'resource "aws_instance" "this" {\n  instance_type = "t3.small"\n}\n'
        assert _sizing_from_refined_terraform(main_tf, "", "ec2") == {"instance_type": "t3.small"}

    def test_lambda_parses_memory_mb(self) -> None:
        main_tf = 'resource "aws_lambda_function" "this" {\n  memory_mb = 256\n}\n'
        assert _sizing_from_refined_terraform(main_tf, "", "lambda") == {"memory_mb": 256}

    def test_unreadable_returns_none(self) -> None:
        assert _sizing_from_refined_terraform("resource {} broken", "", "ecs") is None
        assert _sizing_from_refined_terraform("", "", "ecs") is None
        assert _sizing_from_refined_terraform("resource {} broken", "", "ec2") is None


class TestRecomputeDecisionFromRefined:
    def test_rebuilds_ecs_sizing(self) -> None:
        decision = _ecs_decision(task_cpu=1024, task_memory=2048)
        main_tf = 'resource "aws_ecs_task_definition" "t" {\n  cpu = "512"\n  memory = "1024"\n}\n'

        updated = _recompute_decision_from_refined(decision, _tf(main_tf))

        assert updated.sizing == {"task_cpu": 512, "task_memory": 1024}
        assert updated.compute_type == "ecs"

    def test_rebuilds_sizing_from_var_references(self) -> None:
        decision = _ecs_decision(task_cpu=1024, task_memory=2048)
        main_tf = (
            'resource "aws_ecs_task_definition" "t" {\n'
            "  cpu   = var.task_cpu\n"
            "  memory = var.task_memory\n"
            "}\n"
            'resource "aws_ecs_service" "s" {\n'
            "  desired_count = var.desired_count\n"
            "}\n"
        )
        variables_tf = (
            'variable "task_cpu" {\n  default = "4096"\n}\n'
            'variable "task_memory" {\n  default = "8192"\n}\n'
            'variable "desired_count" {\n  default = 3\n}\n'
        )

        updated = _recompute_decision_from_refined(
            decision, TerraformFiles(main_tf=main_tf, variables_tf=variables_tf, outputs_tf="")
        )

        assert updated.sizing == {
            "task_cpu": 4096,
            "task_memory": 8192,
            "desired_count": 3,
        }

    def test_unreadable_keeps_original_decision(self) -> None:
        decision = _ecs_decision(task_cpu=1024, task_memory=2048)

        updated = _recompute_decision_from_refined(decision, _tf("resource {} broken"))

        assert updated is decision
        assert updated.sizing == {"task_cpu": 1024, "task_memory": 2048}


def test_cost_reflects_refined_sizing_after_regen(monkeypatch: pytest.MonkeyPatch) -> None:
    """After Gate-2 regeneration, the reported cost comes from the refiner's
    actual main.tf sizing, not the pre-regen decision."""
    raw = _load_raw("sample_input.json")
    raw["user_feedback"] = "utilise 512MB et 0.5 vCPU pour réduire le coût"

    def _fake_refiner(terraform_files, feedback, *, dockerfile=None, repo_context=None, force_dockerfile=False):
        refined_main = (
            'resource "aws_ecs_task_definition" "t" {\n'
            '  cpu = "512"\n'
            '  memory = "1024"\n'
            "}\n"
        )
        return TerraformFiles(
            main_tf=refined_main,
            variables_tf=terraform_files.variables_tf,
            outputs_tf=terraform_files.outputs_tf,
        ), dockerfile

    monkeypatch.setattr("core.pipeline.refine_terraform", _fake_refiner)

    context = run_pipeline_with_context(raw)

    assert context.decision.sizing == {"task_cpu": 512, "task_memory": 1024}
    assert context.finops.recommended is not None
    assert context.output.aws_config.estimated_monthly_cost.amount > 0


def test_cost_rises_with_var_referenced_sizing_and_desired_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduces the real Gate-2 output: sizing rendered as var references
    with defaults in variables.tf plus desired_count > 1 — the reported cost
    must rise, not stay frozen at the pre-regen $28.83."""
    raw = _load_raw("sample_input.json")
    raw["user_feedback"] = "make it more expensive, i have a very big userbase"

    baseline = run_pipeline_with_context(_load_raw("sample_input.json"))
    baseline_cost = baseline.output.aws_config.estimated_monthly_cost.amount

    def _fake_refiner(terraform_files, feedback, *, dockerfile=None, repo_context=None, force_dockerfile=False):
        refined_main = (
            'resource "aws_ecs_task_definition" "t" {\n'
            "  cpu   = var.task_cpu\n"
            "  memory = var.task_memory\n"
            "}\n"
            'resource "aws_ecs_service" "s" {\n'
            "  desired_count = var.desired_count\n"
            "}\n"
        )
        refined_variables = (
            'variable "task_cpu" {\n  type = string\n  default = "4096"\n}\n'
            'variable "task_memory" {\n  type = string\n  default = "8192"\n}\n'
            'variable "desired_count" {\n  type = number\n  default = 3\n}\n'
        )
        return TerraformFiles(
            main_tf=refined_main,
            variables_tf=refined_variables,
            outputs_tf=terraform_files.outputs_tf,
        ), dockerfile

    monkeypatch.setattr("core.pipeline.refine_terraform", _fake_refiner)

    context = run_pipeline_with_context(raw)

    assert context.decision.sizing == {
        "task_cpu": 4096,
        "task_memory": 8192,
        "desired_count": 3,
    }
    cost = context.output.aws_config.estimated_monthly_cost.amount
    assert cost > baseline_cost, f"cost {cost} should exceed baseline {baseline_cost}"


def test_hidden_first_regen_fix_appended_only_on_first_regen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repo-conformance fix (real container port / health check) is a
    hidden suffix to the user's feedback on the FIRST Gate-2 regen only.
    Later regens must pass exactly what the user typed."""
    from core.pipeline import _HIDDEN_FIRST_REGEN_FIX

    seen: list[str] = []

    def _fake_refiner(terraform_files, feedback, *, dockerfile=None, repo_context=None, force_dockerfile=False):
        seen.append(feedback)
        return terraform_files, dockerfile

    monkeypatch.setattr("core.pipeline.refine_terraform", _fake_refiner)

    raw_first = _load_raw("sample_input.json")
    raw_first["user_feedback"] = "add a honeypot vm"
    raw_first["regen_iteration"] = 1
    run_pipeline_with_context(raw_first)

    raw_second = _load_raw("sample_input.json")
    raw_second["user_feedback"] = "add a honeypot vm"
    raw_second["regen_iteration"] = 2
    run_pipeline_with_context(raw_second)

    assert len(seen) == 2
    assert _HIDDEN_FIRST_REGEN_FIX in seen[0]
    assert seen[0].startswith("add a honeypot vm")
    assert seen[1] == "add a honeypot vm"
    assert _HIDDEN_FIRST_REGEN_FIX not in seen[1]


def test_hidden_fix_not_applied_without_regen_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No regen_iteration (e.g. a directly-driven pipeline run) means no
    hidden suffix — feedback goes through verbatim."""
    from core.pipeline import _HIDDEN_FIRST_REGEN_FIX

    seen: list[str] = []

    def _fake_refiner(terraform_files, feedback, *, dockerfile=None, repo_context=None, force_dockerfile=False):
        seen.append(feedback)
        return terraform_files, dockerfile

    monkeypatch.setattr("core.pipeline.refine_terraform", _fake_refiner)

    raw = _load_raw("sample_input.json")
    raw["user_feedback"] = "use two AZs"
    run_pipeline_with_context(raw)

    assert seen == ["use two AZs"]
    assert _HIDDEN_FIRST_REGEN_FIX not in seen[0]


def _ecs_decision(task_cpu: int, task_memory: int) -> DecisionResult:
    return DecisionResult(
        compute_type="ecs",
        sizing={"task_cpu": task_cpu, "task_memory": task_memory},
        score_breakdown={"ecs": 3.0, "lambda": 0.0, "ec2": 0.0},
    )


def _tf(main_tf: str) -> TerraformFiles:
    return TerraformFiles(main_tf=main_tf, variables_tf="", outputs_tf="")
