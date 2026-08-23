"""
Database persistence helpers for orchestrator state.

Light writes (status + run_metadata on analysis_runs) and heavier child-table
materialization both happen in the request path, since the orchestrator runs
in-process inside the API.
"""
import json
import logging
from datetime import datetime
from typing import Any, cast
from uuid import uuid4

from sqlalchemy.orm import Session

from .models import (
    AgentTask,
    AnalysisRun,
    CodeSecFinding,
    Deployment,
    InfracostEstimate,
    TerraformArtifact,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = ("completed", "rolled_back", "failed", "rejected")

# The codesec agent's Severity enum has "info", but the severity CHECK constraint
# only accepts the classic four; map down so real scans can't 500 on commit.
_ALLOWED_SEVERITIES = ("critical", "high", "medium", "low")


def _normalize_severity(severity: str | None) -> str:
    value = (severity or "").strip().lower()
    mapping = {
        "critical": "critical",
        "high": "high",
        "error": "high",
        "medium": "medium",
        "warning": "medium",
        "low": "low",
        "note": "low",
        "info": "low",
        "informational": "low",
        "unknown": "low",
    }
    return mapping.get(value, "low")


_ALLOWED_SCANNERS = ("semgrep", "gitleaks", "trivy", "bandit")


def _normalize_scanner(scanner: str | None) -> str:
    """Map scanner names into the constrained codesec_findings set (regex-fallback -> gitleaks)."""
    value = (scanner or "").strip().lower()
    if value in _ALLOWED_SCANNERS:
        return value
    return "gitleaks" if "regex" in value else "semgrep"


def _docker_artifacts(state: dict) -> list[tuple[str, str, str | None]]:
    """Docker artifacts from ``state`` as ``(file_path, artifact_type, content)``
    tuples (content None when absent). Real InfraCost: plural ``docker_images``
    at ``infracost_result._deploy_inputs.artifacts``; mock/legacy: singular
    ``dockerfile`` + ``docker_image`` at ``deployops_result.artifacts``.
    """
    artifacts: dict[str, Any] = {}
    infracost = cast(dict[str, Any], state.get("infracost_result") or {})
    deploy_inputs = cast(dict[str, Any], (infracost.get("_deploy_inputs") or {}).get("artifacts") or {})
    deployops = cast(dict[str, Any], (state.get("deployops_result") or {}).get("artifacts") or {})
    for source in (deploy_inputs, deployops):
        for key in ("dockerfile", "docker_image", "docker_images"):
            if key in source:
                artifacts[key] = source[key]

    rows: list[tuple[str, str, str | None]] = []
    plural = artifacts.get("docker_images")
    if plural:
        for image in plural:
            context = (image.get("context") or ".").rstrip("/")
            rel = "Dockerfile" if context == "." else f"{context}/Dockerfile"
            rows.append((rel, "dockerfile", image.get("dockerfile")))
        return rows

    dockerfile = artifacts.get("dockerfile")
    docker_image = cast(dict[str, Any], artifacts.get("docker_image") or {})
    image: dict[str, Any] = {}
    for field, value in (("name", "name"), ("tag", "tag")):
        if docker_image.get(field):
            image[field] = docker_image[field]

    return [
        ("Dockerfile", "dockerfile", dockerfile if dockerfile else None),
        (
            "docker-image.json",
            "docker-image",
            json.dumps(image, indent=2) if image else None,
        ),
    ]


def _artifact_specs(state: dict) -> list[tuple[str, str, str]]:
    """``(file_path, artifact_type, content)`` rows for a state: generated
    Terraform files (nested ``files`` real, flat keys mock) plus docker artifacts."""
    infracost = cast(dict[str, Any], state.get("infracost_result") or {})
    terraform_raw = cast(dict[str, Any], infracost.get("generated_terraform") or {})
    terraform_files = terraform_raw.get("files")
    terraform = terraform_files if isinstance(terraform_files, dict) else terraform_raw
    specs: list[tuple[str, str, str]] = []
    for file_path, content in (
        ("main.tf", terraform.get("main.tf") or terraform.get("main_tf")),
        ("variables.tf", terraform.get("variables.tf") or terraform.get("variables_tf")),
        ("outputs.tf", terraform.get("outputs.tf") or terraform.get("outputs_tf")),
    ):
        if content:
            specs.append((file_path, "terraform", content))
    for file_path, artifact_type, content in _docker_artifacts(state):
        if content is not None:
            specs.append((file_path, artifact_type, content))
    return specs


def _write_artifact_rows(db: Session, run_id: str, state: dict) -> int:
    written = 0
    for file_path, artifact_type, content in _artifact_specs(state):
        db.add(TerraformArtifact(
            run_id=run_id,
            artifact_type=artifact_type,
            file_path=file_path,
            content=content,
        ))
        written += 1
    return written


def coarse_status(orch_status: str) -> str:
    """Map the rich orchestrator status onto the simple AnalysisRun enum."""
    if orch_status == "completed":
        return "completed"
    if orch_status in TERMINAL_STATUSES:
        return "failed"
    return "running"


def _jsonable_interrupt(entry: Any) -> Any:
    """``Interrupt`` -> frontend shape (``__interrupt__[i].value``); dicts pass through."""
    if isinstance(entry, dict):
        return entry
    value = getattr(entry, "value", None)
    if value is not None:
        return {"value": value}
    return entry


def serialize_state(state: dict) -> dict:
    """JSON-safe state preserving ``__interrupt__`` (GateApproval reads its gate
    context; without it the approval card goes blank)."""
    clean = {k: v for k, v in state.items() if not k.startswith("__")}
    interrupt = state.get("__interrupt__")
    if interrupt is not None:
        clean["__interrupt__"] = [_jsonable_interrupt(entry) for entry in interrupt]
    return json.loads(json.dumps(clean, default=str))


def update_run_state(db: Session, run_id: str, state: dict) -> AnalysisRun:
    run = db.query(AnalysisRun).filter(AnalysisRun.id == run_id).first()
    if run is None:
        raise ValueError(f"AnalysisRun {run_id} not found")

    run.status = coarse_status(state.get("status", "running"))  # type: ignore[reportAttributeAccessIssue]
    run.run_metadata = serialize_state(state)  # type: ignore[reportAttributeAccessIssue]
    if state.get("status") in TERMINAL_STATUSES:
        run.completed_at = datetime.utcnow()  # type: ignore[reportAttributeAccessIssue]
        if run.started_at is not None:
            run.duration_seconds = int(  # type: ignore[reportAttributeAccessIssue]
                (run.completed_at - run.started_at).total_seconds()
            )
    db.commit()
    db.refresh(run)
    return run


def persist_results(db: Session, run_id: str, state: dict) -> int:
    """Materialize a completed orchestrator state into the child result tables;
    idempotent (existing rows replaced first). Returns the number of rows written."""
    db.query(Deployment).filter(Deployment.run_id == run_id).delete()
    db.query(TerraformArtifact).filter(TerraformArtifact.run_id == run_id).delete()
    db.query(InfracostEstimate).filter(InfracostEstimate.run_id == run_id).delete()
    db.query(CodeSecFinding).filter(CodeSecFinding.run_id == run_id).delete()
    db.query(AgentTask).filter(AgentTask.run_id == run_id).delete()

    written = 0

    for agent_name, result in {
        "codesec": state.get("codesec_result"),
        "infracost": state.get("infracost_result"),
        "deployops": state.get("deployops_result"),
    }.items():
        if not result:
            continue
        db.add(AgentTask(
            run_id=run_id,
            agent_name=agent_name,
            celery_task_id=str(uuid4()),
            status="success" if result.get("status", "success") != "failed" else "failure",
            started_at=datetime.utcnow(),
            finished_at=datetime.utcnow(),
            retry_count=0,
            raw_result=result,
        ))
        written += 1

    codesec = cast(dict[str, Any], state.get("codesec_result") or {})
    for finding in codesec.get("sast_findings", []) or []:
        db.add(CodeSecFinding(
            run_id=run_id,
            scanner=_normalize_scanner(finding.get("tool", "semgrep")),
            severity=_normalize_severity(finding.get("severity", "low")),
            file_path=finding.get("file", ""),
            line_number=finding.get("line"),
            rule_id=finding.get("rule_id", "unknown"),
            rule_title=finding.get("message", "No title"),
            description=finding.get("message", ""),
            remediation_hint=finding.get("remediation"),
            raw_json=finding,
        ))
        written += 1

    # Materialize secrets so they survive past the paused state.
    for finding in codesec.get("secrets", []) or []:
        secret_type = finding.get("type", "secret")
        db.add(CodeSecFinding(
            run_id=run_id,
            scanner=_normalize_scanner(finding.get("tool", "gitleaks")),
            severity=_normalize_severity(finding.get("severity", "high")),
            file_path=finding.get("file", ""),
            line_number=finding.get("line"),
            rule_id=finding.get("rule_id", secret_type),
            rule_title=f"Hardcoded {secret_type} secret",
            description=finding.get("value_preview", ""),
            remediation_hint=finding.get("remediation"),
            raw_json=finding,
        ))
        written += 1

    infracost = cast(dict[str, Any], state.get("infracost_result") or {})
    cost_estimate = cast(dict[str, Any], infracost.get("cost_estimate") or {})
    breakdown = cast(list, cost_estimate.get("breakdown") or [])
    if not breakdown and cost_estimate.get("monthly_cost_usd"):
        breakdown = [{
            "service": "Estimated total",
            "monthly_cost_usd": cost_estimate["monthly_cost_usd"],
        }]
    for item in breakdown:
        if not isinstance(item, dict):
            continue
        monthly = item.get("monthly_cost_usd", 0)
        db.add(InfracostEstimate(
            run_id=run_id,
            resource_type=item.get("service", "unknown"),
            resource_name=item.get("service", "unknown"),
            monthly_cost_usd=monthly,
            annual_cost_usd=round(monthly * 12, 2),
            usage_assumptions=infracost.get("load_scenarios"),
            cost_drivers=infracost.get("optimizations"),
            confidence_level="medium",
        ))
        written += 1

    written += _write_artifact_rows(db, run_id, state)

    deployops = cast(dict[str, Any], state.get("deployops_result") or {})
    status_map = {"success": "succeeded", "failed": "failed", "rolled_back": "rolled_back"}
    deploy_status = status_map.get(deployops.get("deployment_status", "pending"), "failed")
    aws_config = cast(dict[str, Any], deployops.get("aws_config") or {})
    db.add(Deployment(
        run_id=run_id,
        environment="dev",
        aws_region=aws_config.get("region", "us-east-1"),
        status=deploy_status,
        applied_at=datetime.utcnow() if deploy_status == "succeeded" else None,
        rollback_reason=deployops.get("rollback_reason"),
        infrastructure_json=deployops,
        cost_total_monthly=None,
    ))
    written += 1

    db.commit()
    logger.info(f"Persisted {written} result rows for run {run_id}")
    return written


def derive_results_from_state(metadata: dict) -> dict:
    """Build normalized result rows straight from orchestrator state: child
    tables are only materialized at a terminal status (see ``persist_results``),
    so for an in-flight or gate-paused run the UI derives these row shapes
    from ``run_metadata``."""
    from uuid import uuid4

    codesec = cast(dict[str, Any], metadata.get("codesec_result") or {})
    findings = []
    for finding in list(codesec.get("sast_findings") or []) + list(codesec.get("secrets") or []):
        scanner = _normalize_scanner(finding.get("tool") or finding.get("scanner") or "semgrep")
        findings.append({
            "id": str(uuid4()),
            "run_id": metadata.get("job_id"),
            "scanner": scanner,
            "severity": finding.get("severity", "info"),
            "file_path": finding.get("file", ""),
            "line_number": finding.get("line"),
            "rule_id": finding.get("rule_id", scanner),
            "rule_title": finding.get("message", "Finding"),
            "description": finding.get("message", ""),
            "remediation_hint": finding.get("remediation"),
            "created_at": datetime.utcnow().isoformat(),
        })

    infracost = cast(dict[str, Any], metadata.get("infracost_result") or {})
    cost_estimate = cast(dict[str, Any], infracost.get("cost_estimate") or {})

    def _estimate_row(name: str, res_type: str, monthly: float) -> dict:
        return {
            "id": str(uuid4()),
            "run_id": metadata.get("job_id"),
            "resource_type": res_type,
            "resource_name": name,
            "monthly_cost_usd": monthly,
            "annual_cost_usd": round(monthly * 12, 2),
            "usage_assumptions": infracost.get("load_scenarios"),
            "cost_drivers": infracost.get("optimizations"),
            "confidence_level": "medium",
            "created_at": datetime.utcnow().isoformat(),
        }

    estimates = []
    for item in cost_estimate.get("breakdown") or []:
        if not isinstance(item, dict):
            continue
        monthly = item.get("monthly_cost_usd", 0)
        estimates.append(_estimate_row(
            item.get("service", "unknown"), item.get("service", "unknown"), monthly
        ))
    # Derive a total row when the breakdown is empty so the UI shows non-zero cost.
    if not estimates and cost_estimate.get("monthly_cost_usd"):
        estimates.append(_estimate_row(
            "Estimated total", "total", float(cost_estimate["monthly_cost_usd"])
        ))

    terraform_raw = cast(dict[str, Any], infracost.get("generated_terraform") or {})
    terraform_files = terraform_raw.get("files")
    terraform = terraform_files if isinstance(terraform_files, dict) else terraform_raw
    artifacts = []
    for file_path, key in (
        ("main.tf", "main.tf"),
        ("variables.tf", "variables.tf"),
        ("outputs.tf", "outputs.tf"),
    ):
        content = terraform.get(key) or terraform.get(key.replace(".", "_"))
        if content:
            artifacts.append({
                "id": str(uuid4()),
                "run_id": metadata.get("job_id"),
                "artifact_type": "terraform",
                "file_path": file_path,
                "content": content,
                "checksum": None,
                "created_at": datetime.utcnow().isoformat(),
            })

    for file_path, artifact_type, content in _docker_artifacts(metadata):
        if content is not None:
            artifacts.append({
                "id": str(uuid4()),
                "run_id": metadata.get("job_id"),
                "artifact_type": artifact_type,
                "file_path": file_path,
                "content": content,
                "checksum": None,
                "edited_by": None,
                "edited_at": None,
                "created_at": datetime.utcnow().isoformat(),
            })

    return {
        "codesec_findings": findings,
        "infracost_estimates": estimates,
        "terraform_artifacts": artifacts,
    }