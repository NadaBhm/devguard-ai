"""Step 9 of the InfraCost pipeline: orchestrate modules 1-4, 6, 7 and 10.

Architecture decision (module 2) now goes through
``decide_architecture_via_llm`` (Phase B): an LLM picks ``compute_type``
when ``OPENROUTER_API_KEY`` is set and the call succeeds, otherwise it
transparently falls back to ``decide_architecture``'s deterministic scoring
— see ``core/llm_architecture_advisor.py`` for the full contract. The
Terraform deployment context (region/environment, module 3's inputs) goes
through ``decide_deployment_context`` (Phase C) the same way — see
``core/llm_deployment_advisor.py``.

A single synchronous entry point, ``run_pipeline``, calling every module in
order. Deliberately a plain function, not ``async def``: the shared
orchestrator (``src/subgroup2/orchestrator/graph.py``) integrates every
agent as a synchronous, in-process call today — no agent anywhere in this
project is async yet. Any future async caller can wrap this in
``asyncio.to_thread(run_pipeline, raw)`` without this module changing at
all; making it async pre-emptively, with no async caller to justify it,
would be the wrong default.

Error handling is precise per stage: module 1's own typed exceptions
(``InvalidStatusError``, ``LowConfidenceError``, ...) already name exactly
what went wrong and carry a ``job_id`` — they propagate unwrapped, since
wrapping them would only obscure that detail. Every other stage's failure
is wrapped in ``PipelineStageError``, naming which stage failed — never a
bare, ambiguous traceback.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Final

from core.cost_estimator import estimate_cost
from core.decision_engine import DecisionResult
from core.finops_optimizer import FinOpsRecommendation, optimize_finops
from core.input_validator import validate_input
from core.llm_architecture_advisor import decide_architecture_via_llm
from core.llm_deployment_advisor import decide_deployment_context
from core.llm_enrichment import build_enrichment
from core.llm_terraform_refiner import _fix_dev_mode_cmd, refine_terraform
from core.output_builder import build_output, resolve_docker_artifacts
from core.repo_ingestor import ingest_repo
from core.terraform_generator import generate_terraform
from models.output_schema import InfraCostOutput, TerraformFiles
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Hidden instruction for first Gate-2 regen and first try: fix port/health path
# and generate a complete Dockerfile from repo context (template defaults
# 8080/"/health" and bare COPY stub fail for most apps).
_HIDDEN_FIRST_REGEN_FIX: Final[str] = (
    "Additionally, correct the container port and the health-check path to "
    "match the actual server configuration found in the repo context (use the "
    "real listen port and the real health route the app exposes; for a "
    "Next.js/Express app that is typically 3000 and /api/health). Keep every "
    "other resource and setting strictly identical."
)

_FIRST_TRY_ARTIFACT_FIX: Final[str] = (
    "Generate a complete, runnable Dockerfile for the application described in "
    "the repo context: choose the correct base image for the detected stack "
    "(e.g. node for React/Next.js, php for PHP, python for Python), install "
    "the real dependencies found in the repo context (only referencing "
    "manifest files that actually exist there), expose the actual listen "
    "port, and set the correct CMD/ENTRYPOINT to start the server. Also "
    "correct the container port and health-check path to match the real "
    "server configuration (the actual listen port and the health route the "
    "app exposes). Keep every other resource and setting strictly identical."
)


# Fallback health path: template defaults to "/health" but most apps use "/"
# or "/api/health". Deterministically infer from repo/Dockerfile.
_HEALTH_PATH_PATTERNS: Final[tuple[str, ...]] = (
    "/health",
    "/api/health",
    "/healthz",
    "/api/healthz",
    "/ready",
    "/api/ready",
    "/api",
    "/",
)
_HEALTH_PATH_HINTS: Final[tuple[str, ...]] = (
    "health",
    "ready",
    "status",
    "ping",
    "alive",
)


def _infer_health_path(primary_dockerfile: str | None, repo_path: str | None) -> str | None:
    """Pick a health-check path the app actually serves.

    Order: repo health route, Dockerfile CMD, fallback "/".
    """
    candidates: list[str] = list(_HEALTH_PATH_PATTERNS)

    # Strongest evidence first: the repo itself declares a health route.
    if repo_path:
        try:
            for p in Path(repo_path).rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(repo_path).as_posix().lower()
                if any(h in rel for h in _HEALTH_PATH_HINTS):
                    for pat in ("/health", "/api/health", "/healthz", "/api/healthz"):
                        if pat in candidates:
                            return pat
                    return "/"
        except Exception:
            pass

    # Dockerfile evidence: only the CMD/ENTRYPOINT line -- scanning the whole
    # file is wrong (e.g. "COPY . /app" would match "/"). Skip the stub
    # "FROM python:3.12-slim COPY . /app" too: it has no CMD.
    if primary_dockerfile:
        cmd = ""
        for line in primary_dockerfile.splitlines():
            upper = line.strip().upper()
            if upper.startswith("CMD") or upper.startswith("ENTRYPOINT"):
                cmd = line
                break
        if cmd:
            for hint in _HEALTH_PATH_HINTS:
                for pat in candidates:
                    if pat in cmd and hint in cmd:
                        return pat
            for pat in candidates:
                if pat in cmd:
                    return pat

    return "/"


def _apply_inferred_health_path(
    terraform_files: TerraformFiles, primary_dockerfile: str | None, repo_path: str | None
) -> None:
    """Rewrite the ECS target group's health-check path in place when the app
    clearly exposes one. Idempotent and fail-soft."""
    try:
        main_tf = terraform_files.main_tf
        inferred = _infer_health_path(primary_dockerfile, repo_path)
        if inferred is None:
            return
        new_tf, n = re.subn(
            r'(path\s*=\s*")[^"]*(")',
            rf'\g<1>{inferred}\g<2>',
            main_tf,
        )
        if n:
            terraform_files.main_tf = new_tf
            logger.info("Inferred health-check path %s from the app", inferred)
    except Exception as exc:
        logger.warning("Health-path inference failed: %s", exc)


def _ensure_production_build(
    dockerfile: str, repo_path: str | None
) -> str:
    """Inject RUN npm run build before CMD when the production server needs it.

    Only for Dockerfiles running next/nuxt/npm start that have a build script
    and no existing build step. Fail-soft.
    """
    if not dockerfile or not repo_path:
        return dockerfile
    try:
        lines = dockerfile.splitlines()
        cmd_index = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("CMD") or stripped.startswith("ENTRYPOINT"):
                cmd_index = i
                break
        if cmd_index is None:
            return dockerfile
        normalized = lines[cmd_index].strip()
        prefix = "CMD" if normalized.startswith("CMD") else "ENTRYPOINT"
        arg_text = normalized[len(prefix):]
        if re.search(r'^\s*\[', arg_text) and "]" in arg_text:
            arg_text = re.sub(r"[\"']", "", arg_text)
            arg_text = arg_text.replace(",", " ").replace("[", " ").replace("]", " ")
        normalized = " ".join(arg_text.split())
        needs_build = re.search(
            r"\b(next|nuxt|vite|vuepress|remix|gatsby|sveltekit|astro)\s+start\b",
            normalized,
        ) or re.search(r"\bnpm\s+(run\s+)?start\b", normalized)
        if not needs_build:
            return dockerfile

        package_json = Path(repo_path) / "package.json"
        if not package_json.is_file():
            return dockerfile
        scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts") or {}
        if not isinstance(scripts, dict) or not scripts.get("build"):
            return dockerfile

        has_build_run = any(
            re.search(r"\bnpm\s+(run\s+)?build\b|next build|nuxt build|vite build", ln)
            for ln in lines
        )
        if has_build_run:
            return dockerfile

        build_line = "RUN npm run build"
        out = lines[:cmd_index] + [build_line] + lines[cmd_index:]
        logger.info("Injected production build step before CMD")
        return "\n".join(out)
    except Exception as exc:
        logger.warning("Production-build injection failed: %s", exc)
        return dockerfile


# Env vars that are conventionally harmless / auto-provisioned by the runtime
# (port, NODE_ENV, path, ...) — never worth a Gate-2 warning.
_IGNORED_ENV_VARS: Final[frozenset[str]] = frozenset({
    "PORT", "HOST", "NODE_ENV", "ENV", "PATH", "HOME", "PWD", "LANG", "TZ",
    "USER", "SHELL", "TERM", "DEBUG", "LOG_LEVEL", "NODE_OPTIONS",
})
_ENV_REF_PATTERN: Final[re.Pattern[str]] = re.compile(r"\$\{?([A-Z][A-Z0-9_]*)\}?")


def _required_env_vars(primary_dockerfile: str | None) -> list[str]:
    """Extract env vars the app hard-requires at boot.

    Scans the primary Dockerfile's CMD/ENTRYPOINT for ``$VAR`` / ``${VAR}``
    references and drops vars the Dockerfile itself sets via ``ENV`` (they
    have a value already) plus the conventional safe set. These are the vars
    a deployment must provide or the container exits and the health check
    rolls back — surfaced at Gate 2 so the user knows before approving.
    Fail-soft: returns [] on any parse hiccup.
    """
    if not primary_dockerfile:
        return []
    defined: set[str] = set()
    for line in primary_dockerfile.splitlines():
        m = re.match(r"^\s*ENV\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|\s)", line)
        if m:
            defined.add(m.group(1).upper())
    required: set[str] = set()
    in_run_block = False
    for line in primary_dockerfile.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("CMD") or upper.startswith("ENTRYPOINT"):
            in_run_block = True
        elif not in_run_block:
            continue
        for match in _ENV_REF_PATTERN.finditer(line):
            var = match.group(1).upper()
            if var in _IGNORED_ENV_VARS or var in defined:
                continue
            required.add(var)
        if not stripped.endswith("\\"):
            in_run_block = False
    return sorted(required)

# The refiner may change sizing in main.tf when the user asks for cost changes
# ("use 512MB / 0.25 vCPU"). It renders these as Terraform variables —
# `cpu = var.task_cpu` with the real value as a `default` in variables.tf —
# so cost must resolve both the literal and the var-reference form and be
# re-estimated against what actually ships.
_ECS_CPU_PATTERN = re.compile(r"\bcpu\s*=\s*\"?(\d+)\"?")
_ECS_MEMORY_PATTERN = re.compile(r"\bmemory\s*=\s*\"?(\d+)\"?")
_ECS_DESIRED_COUNT_PATTERN = re.compile(r"\bdesired_count\s*=\s*(\d+)")
_EC2_INSTANCE_PATTERN = re.compile(r"\binstance_type\s*=\s*\"([^\"]+)\"")
_LAMBDA_MEMORY_PATTERN = re.compile(r"\bmemory_mb\s*=\s*\"?(\d+)\"?")
_VAR_REFERENCE_PATTERN = re.compile(r"\bvar\.[a-z_]+")


class PipelineContext(BaseModel):
    """Everything ``run_pipeline()`` computes internally, for callers that
    need more than the final ``InfraCostOutput`` contract exposes.

    Currently used by ``core.orchestrator_adapter``, which needs
    ``decision`` and ``finops`` to derive fields (load scenarios, region
    comparison, FinOps options) that ``InfraCostOutput`` itself never
    carries — see that module's docstring for why.
    """

    output: InfraCostOutput
    decision: DecisionResult
    finops: FinOpsRecommendation
    warnings: list[str] = Field(default_factory=list)


def _content_for_image(
    image: "DockerImage", contents: dict[str, str]
) -> str | None:
    """Map a captured dockerfile path -> content dict onto one image.

    Matches by the image's build context (``context/Dockerfile``), then by an
    exact path entry when the context is the repo root. Returns ``None`` when
    no entry plausibly belongs to this image.
    """
    if not image.context or image.context == ".":
        return contents.get("Dockerfile")
    return contents.get(f"{image.context}/Dockerfile")


class PipelineStageError(Exception):
    """A pipeline stage other than module 1 failed.

    Names exactly which stage, and keeps the original exception attached
    (both as ``original_exception`` and via ``raise ... from``) — never an
    ambiguous, unattributed crash.
    """

    def __init__(self, stage: str, original_exception: Exception) -> None:
        self.stage = stage
        self.original_exception = original_exception
        super().__init__(f"Pipeline failed at stage '{stage}': {original_exception}")


def _resolve_terraform_value(
    main_tf: str, variables_tf: str, literal_pattern: re.Pattern[str], var_name: str
) -> str | None:
    """Resolve a sizing value from refined Terraform.

    Tries the literal form first (``cpu = "512"`` in main.tf), then the
    variable-reference form the refiner produces (``cpu = var.task_cpu`` with
    ``default = "512"`` in variables.tf). Returns ``None`` when neither form
    carries a usable value.
    """
    literal = literal_pattern.search(main_tf)
    if literal:
        return literal.group(1)
    if not _VAR_REFERENCE_PATTERN.search(main_tf):
        return None
    var_block = re.search(
        rf'variable\s+"{re.escape(var_name)}"\s*\{{.*?default\s*=\s*("?[^"\n}}]+"?)',
        variables_tf,
        re.DOTALL,
    )
    if not var_block:
        return None
    value = var_block.group(1).strip().strip('"')
    return value or None


def _sizing_from_refined_terraform(
    main_tf: str, variables_tf: str, compute_type: str
) -> dict[str, int | str] | None:
    """Extract sizing back out of a refined ``main.tf``.

    The Gate-2 refiner may rewrite ``cpu``/``memory``/``desired_count`` (ECS),
    ``instance_type`` (EC2) or ``memory_mb`` (lambda) when the user asks for
    cost changes. Cost must be re-estimated against those real values, not the
    pre-regen decision. Returns the extracted sizing dict, or ``None`` when the
    files don't carry usable values (fail-soft: keep the original decision).
    """
    try:
        if compute_type == "ecs":
            cpu = _resolve_terraform_value(main_tf, variables_tf, _ECS_CPU_PATTERN, "task_cpu")
            memory = _resolve_terraform_value(
                main_tf, variables_tf, _ECS_MEMORY_PATTERN, "task_memory"
            )
            if not cpu or not memory:
                return None
            sizing: dict[str, int | str] = {
                "task_cpu": int(cpu),
                "task_memory": int(memory),
            }
            desired = _resolve_terraform_value(
                main_tf, variables_tf, _ECS_DESIRED_COUNT_PATTERN, "desired_count"
            )
            if desired:
                sizing["desired_count"] = int(desired)
            return sizing
        if compute_type == "ec2":
            inst = _EC2_INSTANCE_PATTERN.search(main_tf)
            if not inst:
                return None
            return {"instance_type": inst.group(1)}
        if compute_type == "lambda":
            mem = _resolve_terraform_value(
                main_tf, variables_tf, _LAMBDA_MEMORY_PATTERN, "memory_mb"
            )
            if not mem:
                return None
            return {"memory_mb": int(mem)}
        if compute_type == "s3":
            # No compute sizing to extract — S3 scales on storage, not CPU.
            return {}
    except Exception:
        pass
    return None


def _recompute_decision_from_refined(
    decision: DecisionResult, terraform_files: TerraformFiles
) -> DecisionResult:
    """Rebuild the decision from the refiner's actual Terraform output.

    Returns the original ``decision`` unchanged when the refined Terraform
    carries no readable sizing (fail-soft — cost then matches the pre-regen
    decision, which is still internally consistent).
    """
    sizing = _sizing_from_refined_terraform(
        terraform_files.main_tf, terraform_files.variables_tf, decision.compute_type
    )
    if not sizing:
        return decision
    return decision.model_copy(update={"sizing": sizing})


def _run_pipeline_internal(raw: dict) -> PipelineContext:
    """The actual pipeline. Both public entry points below call this and
    only differ in how much of the result they hand back — the steps
    themselves, and every error-handling decision, live here exactly once.
    """
    analysis = validate_input(raw)  # not wrapped: already typed + carries job_id

    # Whole-repo digestion for the OpenRouter LLM advisors and the Terraform
    # refiner, so they see the real code -- on EVERY pass, not just Gate-2
    # regeneration. The orchestrator re-clones the repo for InfraCost and
    # threads repo_path through; on the first try that lets the LLM generate
    # a runnable Dockerfile / correct port / health path from the actual app
    # instead of shipping the deterministic stub and waiting for a regen.
    # Fail-soft by design: no repo_path, no digest, or any failure -> the
    # pipeline proceeds exactly as before (repo_context stays None).
    if raw.get("repo_path"):
        try:
            repo_context = ingest_repo(
                Path(raw["repo_path"]),
                analysis.job_id,
                commit_sha=analysis.repo_metadata.commit_sha,
            )
        except Exception as exc:
            logger.warning(
                "[%s] Repo digestion failed; proceeding without repo context: %s",
                analysis.job_id, exc,
            )
            repo_context = None
        if repo_context:
            analysis = analysis.model_copy(update={"repo_context": repo_context})

    try:
        decision = decide_architecture_via_llm(analysis)
    except Exception as exc:
        raise PipelineStageError("decision_engine", exc) from exc

    try:
        docker_images = resolve_docker_artifacts(analysis, decision)
        # Prefer the real Dockerfile content the CodeSec agent extracted
        # (agent.payload["dockerfile_content"] / ["dockerfile_contents"]),
        # never a synthesized stand-in. Plural dict (path -> content) maps
        # onto each image by build context; singular string still overrides
        # the primary (first) image for legacy payloads.
        raw_contents = raw.get("dockerfile_contents")
        # Whether at least one image carries a real Dockerfile CodeSec
        # captured (vs. the synthesized "FROM ... COPY . /app" stub). The
        # first-try artifact fix only needs to regenerate the Dockerfile when
        # it's a stub: regenerating a real one from the repo digest makes the
        # LLM rewrite a working Dockerfile from scratch, and it has corrupted
        # valid ones before (splicing a stray "RUN apk add" into an apt-get
        # block -> build died). Real Dockerfiles still get the port/health
        # correction pass, just not a full regeneration.
        has_real_dockerfile = False
        if isinstance(raw_contents, dict) and raw_contents:
            for image in docker_images:
                match = _content_for_image(image, raw_contents)
                if match:
                    image.dockerfile = match
                    has_real_dockerfile = True
        elif raw.get("dockerfile_content"):
            for image in docker_images:
                image.dockerfile = raw["dockerfile_content"]
            has_real_dockerfile = bool(raw.get("dockerfile_content"))
        # Extract each image's real listen port from its own Dockerfile's
        # EXPOSE line, instead of always wiring the ECS template's fixed
        # default (8080) into both the container's containerPort and the
        # ALB target group. Confirmed mismatch in practice: a FastAPI app
        # on port 8000 got 8080 wired in, nothing ever answered there, and
        # every health check failed with 502 -> full rollback. Fail-soft:
        # None (no EXPOSE line, e.g. the generic synthesized Dockerfile)
        # falls back to ECS_HEALTH_CHECK_PORT in terraform_generator.py.
        primary_image = docker_images[0] if docker_images else None
        # Extract each container's real listen port from its own Dockerfile's
        # EXPOSE line (same reasoning as the singular port below). None falls
        # back to ECS_HEALTH_CHECK_PORT in terraform_generator.py.
        def _expose_port(image) -> int | None:
            match = (
                re.search(r"^\s*EXPOSE\s+(\d+)", image.dockerfile, re.MULTILINE)
                if image and image.dockerfile
                else None
            )
            return int(match.group(1)) if match else None

        image_ports = {img.name: _expose_port(img) for img in docker_images}
        primary_port = image_ports.get(primary_image.name) if primary_image else None
        terraform_context = decide_deployment_context(
            analysis,
            job_id=analysis.job_id,
            docker_image=(
                f"{primary_image.name}:{primary_image.tag}" if primary_image else None
            ),
            health_check_port=primary_port,
        )
        # A bare "name:tag" resolves against Docker Hub by default, not our
        # ECR repo, so ECS fails with CannotPullContainerError. Qualify every
        # image with the ECR registry host once account_id and region (the
        # latter decided above by decide_deployment_context) are both known.
        # account_id may be absent (STS call failed upstream in the
        # orchestrator adapter) — fail-soft and keep the bare name rather
        # than raise, same policy as the rest of this pipeline.
        if terraform_context.docker_image and analysis.account_id:
            ecr_registry = f"{analysis.account_id}.dkr.ecr.{terraform_context.region}.amazonaws.com"
            terraform_context.docker_image = f"{ecr_registry}/{terraform_context.docker_image}"
            terraform_context.docker_images = [
                {
                    "name": img.name,
                    "image": f"{ecr_registry}/{img.name}:{img.tag}",
                    "port": image_ports[img.name],
                    "context": img.context,
                }
                for img in docker_images
            ]
        elif docker_images:
            terraform_context.docker_images = [
                {
                    "name": img.name,
                    "image": f"{img.name}:{img.tag}",
                    "port": image_ports[img.name],
                    "context": img.context,
                }
                for img in docker_images
            ]
        terraform_files = generate_terraform(decision, terraform_context)
        # LLM artifact pass: let the LLM edit the rendered files (and the
        # effective Dockerfile) so they match the real app. Runs on the FIRST
        # try too -- not just Gate-2 feedback -- so the artifacts aren't the
        # deterministic stub (bare "FROM python:3.12-slim COPY . /app", hard
        # coded 8080/"/health") that can't run the app. Fail-soft: unchanged
        # files if the refiner can't run.
        force_dockerfile = False
        if analysis.user_feedback:
            feedback = analysis.user_feedback
            # First regen only: append the hidden repo-conformance fix so the
            # container port / health check match the app instead of the
            # template's hardcoded 8080 + "/health". Later regens (2+) keep
            # exactly what the user typed — their prompts own the artifacts.
            if int(raw.get("regen_iteration") or 0) == 1:
                feedback = f"{feedback}\n\n{_HIDDEN_FIRST_REGEN_FIX}"
        elif raw.get("repo_path"):
            # First try (no feedback yet): drive a repo-conformant Dockerfile
            # and correct port/health from the whole-repo digest instead of
            # shipping the stub and waiting for a regen. Only regenerate the
            # Dockerfile when it's a stub -- a real captured Dockerfile is
            # trusted as-is (the refiner has corrupted valid ones) and only
            # gets the port/health correction.
            if has_real_dockerfile:
                feedback = _HIDDEN_FIRST_REGEN_FIX
                force_dockerfile = False
            else:
                feedback = _FIRST_TRY_ARTIFACT_FIX
                force_dockerfile = True
        else:
            feedback = None

        if feedback:
            if len(docker_images) > 1:
                # Multi-container: the refiner edits every Dockerfile (keyed by
                # build context path) with per-file sanitization.
                dockerfile_map = {
                    (img.context.rstrip("/") + "/Dockerfile" if img.context != "." else "Dockerfile"):
                    img.dockerfile
                    for img in docker_images
                }
                terraform_files, refined_map = refine_terraform(
                    terraform_files,
                    feedback,
                    dockerfiles=dockerfile_map,
                    repo_context=analysis.repo_context,
                    force_dockerfile=force_dockerfile,
                )
                if refined_map:
                    for image in docker_images:
                        key = (
                            image.context.rstrip("/") + "/Dockerfile"
                            if image.context != "." else "Dockerfile"
                        )
                        if key in refined_map:
                            image.dockerfile = refined_map[key]
            else:
                primary_dockerfile = primary_image.dockerfile if primary_image else None
                terraform_files, primary_dockerfile = refine_terraform(
                    terraform_files,
                    feedback,
                    dockerfile=primary_dockerfile,
                    repo_context=analysis.repo_context,
                    force_dockerfile=force_dockerfile,
                )
                if primary_image:
                    primary_image.dockerfile = primary_dockerfile
    except Exception as exc:
        raise PipelineStageError("terraform_generator", exc) from exc

    # Deterministic health-path inference: the refiner is asked to correct the
    # path but is unreliable on it, so fall back to reading the app itself.
    # Runs on every render (first try and regens) so the path is right even
    # when the LLM leaves the template's "/health" in place.
    _apply_inferred_health_path(
        terraform_files,
        primary_image.dockerfile if primary_image else None,
        raw.get("repo_path"),
    )

    # Fix dev-mode CMDs to production equivalents (fail-soft).
    if primary_image and primary_image.dockerfile:
        primary_image.dockerfile = _fix_dev_mode_cmd(primary_image.dockerfile)
        primary_image.dockerfile = _ensure_production_build(
            primary_image.dockerfile, raw.get("repo_path")
        )
    for image in docker_images:
        if image.dockerfile:
            image.dockerfile = _fix_dev_mode_cmd(image.dockerfile)
            image.dockerfile = _ensure_production_build(
                image.dockerfile, raw.get("repo_path")
            )

    # Boot-blocking env vars: apps like Animetrix hard-exit unless secrets
    # (MONGODB_URI, JWT_TOKEN) are set, so the container never reaches healthy.
    # Can't conjure the values, but must surface them at Gate 2 rather than
    # let the deploy silently roll back.
    required_env_vars = _required_env_vars(
        primary_image.dockerfile if primary_image else None
    )
    warnings = (
        [
            f"App requires env var(s) at boot: {', '.join(required_env_vars)}. "
            "Deployment will not go healthy until these are provided."
        ]
        if required_env_vars
        else []
    )

    # A detected database is declared in terraform but NEVER provisioned: the
    # ECS template wires DB_* env vars from tfvars (DEVGUARD_DB_*), so the app
    # cannot reach healthy unless a database already exists somewhere the
    # deployer controls. Surface it at Gate 2 instead of letting the deploy
    # fail later on an obscure "required variable" error.
    # sqlite is a local file, not a server — no managed DB needed.
    if analysis.stack_detection.database and analysis.stack_detection.database != "sqlite":
        warnings.append(
            f"App uses a {analysis.stack_detection.database} database — a managed DB "
            f"(db.t3.micro) will be provisioned alongside the app."
        )

    # After Gate-2 feedback the refiner may have rewritten sizing in main.tf;
    # cost must reflect what actually ships, not the pre-regen decision.
    # Fail-soft: if the refined Terraform carries no readable sizing, this
    # returns the original decision and cost stays unchanged. The recomputed
    # decision is threaded through every downstream stage (finops, enrichment,
    # output) so the reported sizing, cost and FinOps scenarios stay in sync.
    refined_decision = _recompute_decision_from_refined(decision, terraform_files)
    try:
        cost = estimate_cost(refined_decision)
    except Exception as exc:
        raise PipelineStageError("cost_estimator", exc) from exc

    try:
        finops = optimize_finops(analysis, refined_decision)
    except Exception as exc:
        raise PipelineStageError("finops_optimizer", exc) from exc

    try:
        enrichment = build_enrichment(refined_decision, cost, finops)
    except Exception as exc:
        raise PipelineStageError("llm_enrichment", exc) from exc

    try:
        output = build_output(
            analysis,
            refined_decision,
            terraform_files,
            cost,
            enrichment,
            docker_images=docker_images,
            region=terraform_context.region,
            environment=terraform_context.environment,
        )
    except Exception as exc:
        raise PipelineStageError("output_builder", exc) from exc

    return PipelineContext(output=output, decision=refined_decision, finops=finops, warnings=warnings)


def run_pipeline(raw: dict) -> InfraCostOutput:
    """Run the full InfraCost pipeline on a raw Agent 1 payload.

    Args:
        raw: Agent 1's raw analysis payload (already-decoded JSON).

    Returns:
        The final ``InfraCostOutput`` contract.

    Raises:
        InputValidationError: (or a subclass) module 1 rejected the
            payload — propagates unwrapped, already precisely typed.
        PipelineStageError: any other stage failed; ``.stage`` names which
            one, ``.original_exception`` holds the real cause.
    """
    return _run_pipeline_internal(raw).output


def run_pipeline_with_context(raw: dict) -> PipelineContext:
    """Like ``run_pipeline``, but also returns the internal ``DecisionResult``
    and ``FinOpsRecommendation`` computed for this same job.

    Same arguments and exceptions as ``run_pipeline`` — this is not a
    different pipeline, just a richer view into the one pipeline that
    exists. Intended for ``core.orchestrator_adapter``; the HTTP endpoint
    in ``main.py`` should keep using plain ``run_pipeline``.
    """
    return _run_pipeline_internal(raw)
