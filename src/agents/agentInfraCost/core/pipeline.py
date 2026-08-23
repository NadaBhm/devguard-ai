"""Step 9 of the InfraCost pipeline: orchestrate modules 1-4, 6, 7 and 10.

One synchronous entry point, ``run_pipeline`` (async callers can wrap it in
``asyncio.to_thread``); LLM advisors fall back transparently to deterministic
scoring; module 1's typed exceptions propagate unwrapped, every other stage
failure wraps in ``PipelineStageError`` naming the stage.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Final

from core.cost_estimator import estimate_cost
from core.decision_engine import DecisionResult, decide_architecture
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

# Hidden instruction for first Gate-2 regen and first try: fix port/health path and
# generate a complete runnable Dockerfile from repo context (stub defaults fail).
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
    """Pick a health-check path the app actually serves:
    repo health route, then Dockerfile CMD, then fallback "/"."""
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

    # Only the CMD/ENTRYPOINT line counts (whole-file scan mismatches, e.g.
    # "COPY . /app"); skip the bare stub too (no CMD).
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


def _apply_inferred_health_port(
    terraform_files: TerraformFiles, primary_dockerfile: str | None
) -> None:
    """Align every rendered health-check port with the app's real listen
    port (CMD --port/-p wins over EXPOSE). Idempotent and fail-soft."""
    if not primary_dockerfile:
        return
    try:
        m_cmd = re.search(
            r'(?mi)^CMD\b.*?(?:--port|-p)\s*[",\s]*"?(\d+)', primary_dockerfile
        )
        m_expose = re.search(r"(?mi)^\s*EXPOSE\s+(\d+)", primary_dockerfile)
        # No signal, or a degenerate "0" extracted -> leave default untouched.
        if not m_cmd and not m_expose:
            return
        new_port = int(m_cmd.group(1)) if m_cmd else int(m_expose.group(1))
        if not new_port:
            return
        cur = re.search(r"(?im)^\s*port\s*=\s*(\d+)", terraform_files.main_tf)
        if not cur:
            return
        old_port = cur.group(1)
        if old_port == str(new_port) or old_port == "5432":
            return
        pat = re.compile(rf"\b{old_port}\b")
        out_lines = []
        for line in terraform_files.main_tf.splitlines():
            if "port" in line.lower():
                out_lines.append(pat.sub(str(new_port), line))
            else:
                out_lines.append(line)
        terraform_files.main_tf = "\n".join(out_lines)
        logger.info("Aligned health-check port %s -> %s from Dockerfile", old_port, new_port)
    except Exception as exc:
        logger.warning("Health-port inference failed: %s", exc)


def _ensure_production_build(
    dockerfile: str, repo_path: str | None
) -> str:
    """Inject RUN npm run build before CMD when needed: only for next/nuxt/npm
    start Dockerfiles whose package.json has a build script and no build step.
    Fail-soft."""
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
    """Extract env vars the app hard-requires at boot: scans CMD/ENTRYPOINT for
    $VAR/${VAR}, dropping vars the Dockerfile ENVs itself plus the conventional
    safe set. Deployment must provide these or the container exits and rolls
    back — surfaced at Gate 2. Fail-soft: returns [] on any parse hiccup."""
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

# The refiner may rewrite sizing as Terraform vars (`cpu = var.task_cpu` with a
# default in variables.tf) — resolve literal and var-reference forms, re-estimate.
_ECS_CPU_PATTERN = re.compile(r"\bcpu\s*=\s*\"?(\d+)\"?")
_ECS_MEMORY_PATTERN = re.compile(r"\bmemory\s*=\s*\"?(\d+)\"?")
_ECS_DESIRED_COUNT_PATTERN = re.compile(r"\bdesired_count\s*=\s*(\d+)")
_EC2_INSTANCE_PATTERN = re.compile(r"\binstance_type\s*=\s*\"([^\"]+)\"")
_LAMBDA_MEMORY_PATTERN = re.compile(r"\bmemory_mb\s*=\s*\"?(\d+)\"?")
_VAR_REFERENCE_PATTERN = re.compile(r"\bvar\.[a-z_]+")


class PipelineContext(BaseModel):
    """Everything ``run_pipeline()`` computes internally beyond ``InfraCostOutput``.
    Used by ``core.orchestrator_adapter``, which derives load scenarios / region
    comparison / FinOps detail from ``decision`` + ``finops`` (not in the output)."""

    output: InfraCostOutput
    decision: DecisionResult
    finops: FinOpsRecommendation
    warnings: list[str] = Field(default_factory=list)


def _content_for_image(
    image: "DockerImage", contents: dict[str, str]
) -> str | None:
    """Map a captured path->content dict onto one image: by build context
    (``context/Dockerfile``), then root ``Dockerfile``; None if nothing fits.
    """
    if not image.context or image.context == ".":
        return contents.get("Dockerfile")
    return contents.get(f"{image.context}/Dockerfile")


class PipelineStageError(Exception):
    """A pipeline stage other than module 1 failed. Names the stage and keeps the
    original exception attached (both as ``original_exception`` and via
    ``raise ... from``) — never an ambiguous, unattributed crash.
    """

    def __init__(self, stage: str, original_exception: Exception) -> None:
        self.stage = stage
        self.original_exception = original_exception
        super().__init__(f"Pipeline failed at stage '{stage}': {original_exception}")


def _resolve_terraform_value(
    main_tf: str, variables_tf: str, literal_pattern: re.Pattern[str], var_name: str
) -> str | None:
    """Resolve a sizing value from refined Terraform: literal form first
    (``cpu = "512"``), then the refiner's var-reference form (``var.task_cpu``
    with a ``default`` in variables.tf). None when neither carries a value.
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
    """Extract sizing back out of a refined ``main.tf`` (the refiner may rewrite
    cpu/memory/desired_count/instance_type/memory_mb on cost requests) so cost
    tracks what actually ships; None when unreadable (keep original decision).
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
    """Rebuild the decision from the refiner's actual Terraform output; returns
    the original unchanged when sizing is unreadable (fail-soft, stays consistent).
    """
    sizing = _sizing_from_refined_terraform(
        terraform_files.main_tf, terraform_files.variables_tf, decision.compute_type
    )
    if not sizing:
        return decision
    return decision.model_copy(update={"sizing": sizing})


def _run_pipeline_internal(raw: dict) -> PipelineContext:
    """The actual pipeline; both public entry points call this and differ only in
    how much of the result they hand back. Steps and error handling live here once.
    """
    analysis = validate_input(raw)  # not wrapped: already typed + carries job_id

    # Whole-repo digestion on EVERY pass (not just regens): the orchestrator threads
    # repo_path so try #1 builds a runnable Dockerfile; failures proceed as before.
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
        # Fast-path: a bare static site is always S3 — skip the LLM advisors.
        from core.decision_engine import _is_static_site
        if _is_static_site(analysis):
            decision = decide_architecture(analysis)
            decision.decision_source = "deterministic"
            logger.info("Static site fast-path: deterministic S3 decision, LLM advisor skipped")
        else:
            decision = decide_architecture_via_llm(analysis)
    except Exception as exc:
        raise PipelineStageError("decision_engine", exc) from exc

    try:
        docker_images = resolve_docker_artifacts(analysis, decision)
        # Prefer real CodeSec-captured Dockerfile content over a synthesized stand-in;
        # plural dict keys by build context, singular string hits the primary image.
        raw_contents = raw.get("dockerfile_contents")
        # Only stubs get full regeneration (a prior regen corrupted a valid Dockerfile
        # by splicing "RUN apk add" into apt-get); real ones keep the port/health pass.
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
        # Per-image EXPOSE port beats the fixed template 8080 (a FastAPI app on
        # 8000 once got 8080 -> 502 health checks -> full rollback); None (no
        # EXPOSE, i.e. the stub) falls back to ECS_HEALTH_CHECK_PORT.
        primary_image = docker_images[0] if docker_images else None
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
        # Bare "name:tag" pulls from Docker Hub (CannotPullContainerError) — qualify
        # with the ECR registry host; missing account_id keeps the bare name (fail-soft).
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
        # LLM artifact pass runs on the FIRST try too, so artifacts aren't the
        # unrunnable deterministic stub (FROM+COPY, hardcoded 8080/"/health"). Fail-soft.
        force_dockerfile = False
        if analysis.user_feedback:
            feedback = analysis.user_feedback
            # First regen only: append the hidden repo-conformance fix (port /
            # health path). Later regens keep exactly what the user typed.
            if int(raw.get("regen_iteration") or 0) == 1:
                feedback = f"{feedback}\n\n{_HIDDEN_FIRST_REGEN_FIX}"
        elif raw.get("repo_path"):
            # First try: drive a repo-conformant Dockerfile + correct port/health
            # from the digest; only stubs regenerate fully (real ones are trusted).
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

    # Deterministic health-path inference: the refiner is unreliable at correcting
    # the path, so read it from the app itself on every render (first try + regens).
    _apply_inferred_health_path(
        terraform_files,
        primary_image.dockerfile if primary_image else None,
        raw.get("repo_path"),
    )
    _apply_inferred_health_port(
        terraform_files,
        primary_image.dockerfile if primary_image else None,
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

    # Boot-blocking env vars (e.g. MONGODB_URI/JWT_TOKEN for apps like Animetrix):
    # surface at Gate 2 rather than let the deploy silently roll back.
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

    # A detected database is declared in Terraform but never provisioned (ECS wires
    # DB_* from tfvars) — warn at Gate 2 unless it's local-file sqlite (no server).
    if analysis.stack_detection.database and analysis.stack_detection.database != "sqlite":
        warnings.append(
            f"App uses a {analysis.stack_detection.database} database — a managed DB "
            f"(db.t3.micro) will be provisioned alongside the app."
        )

    # After Gate-2 feedback the refiner may have rewritten sizing; cost reflects
    # what actually ships (fail-soft keeps the original), threaded downstream.
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
    """Run the full InfraCost pipeline on a raw Agent 1 payload; returns the final
    ``InfraCostOutput``. Raises InputValidationError (module 1 rejected the payload,
    propagated unwrapped, already typed) or PipelineStageError (``.stage`` names the
    stage, ``.original_exception`` holds the cause)."""
    return _run_pipeline_internal(raw).output


def run_pipeline_with_context(raw: dict) -> PipelineContext:
    """Like ``run_pipeline``, but also returns the internal DecisionResult and
    FinOpsRecommendation for the same job — same args/exceptions, just a richer
    view. For ``core.orchestrator_adapter``; main.py keeps plain ``run_pipeline``."""
    return _run_pipeline_internal(raw)
