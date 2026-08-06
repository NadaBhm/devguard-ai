"""
DevGuard AI - Final Report Generation (T-3.12)
================================================
Renders the pipeline's results into a shareable HTML report, and a PDF when
the environment can produce one.

CDC Reference:
    US-2.2.6 "As a user, I want a final report so that I can share results
    with stakeholders. Given a completed deployment, When report generates,
    Then it produces PDF/HTML with all agent results, architecture diagram,
    and cost breakdown."
    NFR: report generation < 10 seconds.

DESIGN NOTES
------------
1. HTML is the primary artifact; PDF is best-effort.
   The CDC tech stack says Jinja2 + WeasyPrint. WeasyPrint is a pure-Python
   package but binds to system libraries (cairo, pango, gdk-pixbuf) that are
   frequently absent - notably on Windows, where several of us develop. A
   hard PDF dependency would mean report generation fails on half the team's
   machines for a format that is a "Should", not a "Must" (US-2.2.6 says
   "PDF/HTML"). So: HTML always succeeds; PDF is attempted and its absence is
   reported in the result rather than raised.

2. The architecture diagram is hand-built SVG, not a rendered image.
   Options were graphviz (system binary), matplotlib (heavy, and a poor fit
   for box diagrams) or diagrams (needs graphviz too). Inline SVG has no
   dependency at all, scales cleanly in print, and WeasyPrint renders it
   natively. The diagram is simple by nature - a handful of labelled boxes -
   so a drawing library would buy nothing.

3. The template is a separate .j2 file, not a string in this module,
   so the frontend owner can restyle the report without touching Python.

4. Secret VALUES are never rendered (only type and location). This report is
   meant to be emailed and archived; embedding the credentials it just found
   would turn a security report into a second leak.

Owner: Hbib (Subgroup 2 - Execution & Control)
"""

from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .state import GRAPH_VERSION, OrchestratorState

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
TEMPLATE_NAME = "report.html.j2"

# Where rendered reports land. Served by the backend at
# /api/jobs/{job_id}/report/download (see download_url below).
DEFAULT_REPORT_DIR = Path(os.getenv("DEVGUARD_REPORT_DIR", "/tmp/devguard-reports"))

# How many findings to table before the report becomes unreadable. The full
# set stays in the job's JSON results; the report is a summary for
# stakeholders, not a dump.
MAX_SAST_ROWS = 15
MAX_SECRET_ROWS = 15
MAX_DEPENDENCY_ROWS = 15


# =============================================================================
# ARCHITECTURE DIAGRAM
# =============================================================================

_ARCH_LAYOUTS: dict[str, list[str]] = {
    "ecs_fargate": ["Internet", "ALB", "ECS Fargate", "RDS"],
    "lambda": ["Internet", "API Gateway", "Lambda", "DynamoDB / RDS"],
    "ec2": ["Internet", "ALB", "EC2 Auto Scaling", "RDS"],
    "hybrid": ["Internet", "ALB", "ECS + Lambda", "RDS"],
}


def build_architecture_svg(architecture: Optional[str], region: str = "") -> str:
    """
    Draw the recommended architecture as a left-to-right box diagram.

    Returns inline SVG (a string), or an empty string if the architecture is
    unknown - an empty diagram is worse than no diagram.
    """
    boxes = _ARCH_LAYOUTS.get(architecture or "")
    if not boxes:
        return ""

    box_w, box_h, gap, pad = 132, 56, 34, 14
    width = pad * 2 + len(boxes) * box_w + (len(boxes) - 1) * gap
    height = pad * 2 + box_h + 26

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="100%" style="max-width:{width}px;font-family:Helvetica,Arial,sans-serif">'
    ]

    for index, label in enumerate(boxes):
        x = pad + index * (box_w + gap)
        y = pad
        fill = "#e6f0f7" if index else "#f5f7fa"
        parts.append(
            f'<rect x="{x}" y="{y}" width="{box_w}" height="{box_h}" rx="7" '
            f'fill="{fill}" stroke="#0b3d5c" stroke-width="1.4"/>'
            f'<text x="{x + box_w / 2}" y="{y + box_h / 2 + 4}" text-anchor="middle" '
            f'font-size="12" fill="#0b3d5c">{html.escape(label)}</text>'
        )
        if index < len(boxes) - 1:
            x1, x2 = x + box_w, x + box_w + gap
            mid_y = y + box_h / 2
            parts.append(
                f'<line x1="{x1}" y1="{mid_y}" x2="{x2 - 7}" y2="{mid_y}" '
                f'stroke="#627d98" stroke-width="1.6"/>'
                f'<polygon points="{x2},{mid_y} {x2 - 8},{mid_y - 4.5} {x2 - 8},{mid_y + 4.5}" '
                f'fill="#627d98"/>'
            )

    if region:
        parts.append(
            f'<text x="{pad}" y="{height - 8}" font-size="10.5" fill="#829ab1">'
            f'AWS region: {html.escape(region)}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# =============================================================================
# STATE -> TEMPLATE CONTEXT
# =============================================================================

def build_template_context(state: OrchestratorState) -> dict[str, Any]:
    """Flatten the orchestrator state into the variables the template expects."""
    codesec = state.get("codesec_result") or {}
    infracost = state.get("infracost_result") or {}
    deployops = state.get("deployops_result") or {}
    metadata = state.get("orchestrator_metadata") or {}

    score_block = codesec.get("security_score") or {}
    summary = codesec.get("summary") or {}
    severity = score_block.get("severity_counts") or {}
    stack_raw = codesec.get("stack_detection") or {}
    cost_estimate = infracost.get("cost_estimate") or {}

    total_findings = sum(
        summary.get(key, 0)
        for key in (
            "sast_findings_count",
            "secrets_found_count",
            "vulnerable_dependencies_count",
            "dockerfile_issues_count",
        )
    )

    # Recommendations: security first, then FinOps.
    recommendations = list(score_block.get("recommendations") or [])
    for option in infracost.get("optimizations") or []:
        description = option.get("description")
        if description:
            recommendations.append(description)

    approvals = []
    for name, gate in (state.get("human_gates") or {}).items():
        approved = gate.get("approved")
        approvals.append({
            "name": name,
            "approved": bool(approved),
            "decision": "approved" if approved else ("rejected" if approved is False else "not reached"),
            "approved_by": gate.get("approved_by"),
            "approved_at": gate.get("approved_at"),
            "comment": gate.get("comment"),
        })

    architecture = infracost.get("architecture_recommendation")
    region = ""
    for entry in infracost.get("region_comparison") or []:
        region = entry.get("region", "")
        break

    return {
        "job_id": state.get("job_id", ""),
        "repo_url": state.get("repo_url", ""),
        "repo_name": (codesec.get("repo_metadata") or {}).get("name")
                     or (state.get("repo_url", "").rstrip("/").split("/")[-1] or "repository"),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "duration_seconds": round(float(metadata.get("elapsed_seconds", 0.0)), 1),
        "graph_version": metadata.get("graph_version", GRAPH_VERSION),
        "nodes_executed": ", ".join(metadata.get("nodes_executed") or []) or "none",

        "has_codesec": bool(codesec),
        "security_score": score_block.get("score", 0),
        "security_grade": score_block.get("grade", "N/A"),
        "total_findings": total_findings,
        "severity": {
            "critical": severity.get("critical", summary.get("total_critical", 0)),
            "high": severity.get("high", summary.get("total_high", 0)),
            "medium": severity.get("medium", summary.get("total_medium", 0)),
            "low": severity.get("low", summary.get("total_low", 0)),
            "info": severity.get("info", summary.get("total_info", 0)),
        },
        "stack": {
            "primary_language": stack_raw.get("primary_language", "unknown"),
            "frameworks": ", ".join(stack_raw.get("frameworks") or []),
            "database": stack_raw.get("database"),
            "container": (stack_raw.get("container") or {}).get("detected", False),
        },
        "sast_findings": (codesec.get("sast_findings") or [])[:MAX_SAST_ROWS],
        "secrets": (codesec.get("secrets") or [])[:MAX_SECRET_ROWS],
        "vulnerable_packages": ((codesec.get("dependencies") or {}).get("vulnerable_packages") or [])[:MAX_DEPENDENCY_ROWS],

        "has_infracost": bool(infracost),
        "architecture": architecture or "not determined",
        "architecture_justification": infracost.get("justification", ""),
        "architecture_svg": build_architecture_svg(architecture, region),
        "monthly_cost": cost_estimate.get("monthly_cost_usd", 0),
        "cost_breakdown": cost_estimate.get("breakdown") or [],
        "load_scenarios": infracost.get("load_scenarios") or [],
        "optimizations": infracost.get("optimizations") or [],
        "region_comparison": infracost.get("region_comparison") or [],

        "has_deployops": bool(deployops),
        "deployment_status": deployops.get("deployment_status", "not deployed"),
        "deployed_url": deployops.get("deployed_url"),
        "health_check_passed": (deployops.get("health_check") or {}).get("passed", False),
        "rollback_triggered": deployops.get("rollback_triggered", False),
        "rollback_reason": deployops.get("rollback_reason"),
        "terraform_outputs": deployops.get("terraform_outputs") or {},

        "approvals": approvals,
        "recommendations": recommendations,
        # Resolved incidents are shown too: "we hit a 503 and recovered" is
        # useful information for whoever reads this, not noise to hide.
        "errors": state.get("error_log") or [],
    }


# =============================================================================
# RENDERING
# =============================================================================

def render_html(state: OrchestratorState) -> str:
    """Render the report to an HTML string."""
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        # Autoescaping is NOT optional here: every string in this report -
        # finding messages, file paths, package names - comes from an
        # arbitrary public repository we just cloned. A finding message of
        # "<script>alert(1)</script>" must render as text, not execute.
        #
        # "j2" MUST stay in this list. select_autoescape() decides from the
        # FILE EXTENSION, and the template is report.html.j2 -> extension
        # ".j2". Passing only ("html", "xml") silently disables escaping for
        # it, which is exactly the bug test_escapes_scanned_content caught.
        autoescape=select_autoescape(
            enabled_extensions=("html", "xml", "j2"),
            default_for_string=True,
        ),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    from markupsafe import Markup

    context = build_template_context(state)
    context["architecture_svg"] = Markup(context["architecture_svg"])
    return env.get_template(TEMPLATE_NAME).render(**context)


def render_pdf(html_content: str, output_path: Path) -> bool:
    """
    Write `html_content` to `output_path` as PDF. Returns whether it worked.

    Never raises: WeasyPrint's system dependencies (cairo/pango) are commonly
    missing, and a missing PDF must not fail a report whose HTML is fine.
    """
    try:
        from weasyprint import HTML  # lazy: importing it costs ~1s

        HTML(string=html_content).write_pdf(str(output_path))
        return True
    except ImportError:
        logger.warning("WeasyPrint not installed - PDF skipped, HTML report is unaffected")
        return False
    except Exception as exc:
        # Usually a missing native library (libpango, libcairo).
        logger.warning("PDF rendering failed (%s) - HTML report is unaffected", exc)
        return False


def generate_report(
    state: OrchestratorState,
    output_dir: Optional[Path] = None,
    *,
    want_pdf: bool = True,
) -> dict[str, Any]:
    """
    Generate the final report for a job. CDC: US-2.2.6

    Returns a FinalReport-shaped dict, extended with the paths actually
    written and which formats succeeded.
    """
    started = datetime.now(timezone.utc)
    job_id = state.get("job_id", "unknown")

    directory = Path(output_dir) if output_dir else DEFAULT_REPORT_DIR
    directory.mkdir(parents=True, exist_ok=True)

    html_content = render_html(state)
    html_path = directory / f"report-{job_id}.html"
    html_path.write_text(html_content, encoding="utf-8")
    logger.info("[%s] HTML report written to %s", job_id, html_path)

    pdf_path = directory / f"report-{job_id}.pdf"
    pdf_ok = render_pdf(html_content, pdf_path) if want_pdf else False
    if not pdf_ok:
        pdf_path.unlink(missing_ok=True)

    context = build_template_context(state)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    logger.info("[%s] Report generated in %.2fs (pdf=%s)", job_id, elapsed, pdf_ok)

    return {
        "format": "pdf" if pdf_ok else "html",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # Served by the backend; the file itself lives at html_path/pdf_path.
        "download_url": f"/api/jobs/{job_id}/report/download",
        "html_path": str(html_path),
        "pdf_path": str(pdf_path) if pdf_ok else None,
        "formats_available": ["html"] + (["pdf"] if pdf_ok else []),
        "render_seconds": round(elapsed, 2),
        "summary": {
            "total_vulnerabilities": context["total_findings"],
            "critical_count": context["severity"]["critical"],
            "estimated_monthly_cost_usd": context["monthly_cost"],
            "deployment_status": context["deployment_status"],
            "recommendations": context["recommendations"],
            "pipeline_duration_seconds": context["duration_seconds"],
        },
    }
