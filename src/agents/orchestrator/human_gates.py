"""
DevGuard AI - Orchestrator Human Approval Gates
==================================================
Human-in-the-loop approval nodes. Each gate pauses the LangGraph workflow
via interrupt() and waits for a human decision (approve/reject), delivered
through resume_workflow(thread_id, resume_data) in graph.py.

CDC Reference: US-2.2.3 (approval gates so the user controls critical actions)

Split out of graph.py (originally Section 4).

Owner: Hbib (Subgroup 2 - Execution & Control)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from langgraph.types import interrupt

from .state import MAX_INFRACOST_ITERATIONS, OrchestratorState

logger = logging.getLogger(__name__)


def human_gate_1_impl(state: OrchestratorState) -> OrchestratorState:
    """Human Approval Gate 1: After CodeSec, before InfraCost. CDC: US-2.2.3"""
    logger.info(f"[{state['job_id']}] HUMAN GATE 1: Pre-InfraCost approval required")

    state["status"] = "awaiting_approval_gate_1"
    state["orchestrator_metadata"]["current_node"] = "human_gate_1"
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    codesec = state.get("codesec_result") or {}
    security_score = codesec.get("security_score", {}).get("score", 0)
    grade = codesec.get("security_score", {}).get("grade", "N/A")
    sast_findings = codesec.get("sast_findings", [])
    secrets = codesec.get("secrets", [])
    recommendations = codesec.get("security_score", {}).get("recommendations", [])

    approval = interrupt({
        "gate": "gate_1_pre_infracost",
        "message": "CodeSec analysis complete. Approve to proceed with infrastructure generation?",
        "context": {
            "security_score": security_score,
            "grade": grade,
            "vulnerabilities_count": len(sast_findings),
            "secrets_count": len(secrets),
            "recommendations": recommendations,
        },
        "actions": ["approve", "reject"],
    })

    gate = state["human_gates"]["gate_1_pre_infracost"]
    gate["approved"] = approval.get("approved", False)
    gate["comment"] = approval.get("comment", "")
    gate["approved_at"] = datetime.now(timezone.utc).isoformat()
    gate["approved_by"] = approval.get("approved_by", "unknown")

    if not gate["approved"]:
        logger.warning(f"[{state['job_id']}] Gate 1 REJECTED. Workflow halted.")
        state["status"] = "rejected"
        state["error_log"].append({
            "node": "human_gate_1",
            "attempt": 1,
            "max_attempts": 1,
            "message": f"Human gate 1 rejected: {approval.get('comment', 'No comment')}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stack_trace": None,
            "resolved": False,
        })
    else:
        logger.info(f"[{state['job_id']}] Gate 1 APPROVED. Proceeding to InfraCost.")

    return state


def human_gate_2_impl(state: OrchestratorState) -> OrchestratorState:
    """Human Approval Gate 2: After InfraCost, before DeployOps. CDC: US-2.2.3"""
    logger.info(f"[{state['job_id']}] HUMAN GATE 2: Pre-DeployOps approval required")

    state["status"] = "awaiting_approval_gate_2"
    state["orchestrator_metadata"]["current_node"] = "human_gate_2"
    state["updated_at"] = datetime.now(timezone.utc).isoformat()

    # A re-pause after a regeneration round must look like a fresh pause:
    # the previous round left approved=False + requested_changes set, which
    # would make the frontend treat this gate as already decided and never
    # show the approval card again. Reset the decision fields in place
    # BEFORE interrupt() - LangGraph only checkpoints in-place mutations of
    # nested dict channels at an interrupt, not scalar rebinds like status.
    gate = state["human_gates"]["gate_2_pre_deployops"]
    gate["approved"] = None
    gate["comment"] = None
    gate["approved_at"] = None
    gate["approved_by"] = None
    gate["requested_changes"] = None

    infracost = state.get("infracost_result") or {}
    cost_estimate = infracost.get("cost_estimate", {})
    monthly_cost = cost_estimate.get("monthly_cost_usd", 0)
    architecture = infracost.get("architecture_recommendation", "unknown")
    breakdown = cost_estimate.get("breakdown", [])
    warnings = infracost.get("warnings", [])

    iterations_done = len(state.get("infracost_iterations", []))
    can_regenerate = iterations_done < MAX_INFRACOST_ITERATIONS
    actions = ["approve", "reject"] + (["regenerate"] if can_regenerate else [])

    approval = interrupt({
        "gate": "gate_2_pre_deployops",
        "message": "InfraCost complete. Review cost estimate and approve deployment?",
        "context": {
            "monthly_cost_usd": monthly_cost,
            "architecture": architecture,
            "breakdown": breakdown,
            "warnings": warnings,
            "iteration": iterations_done,
            "max_iterations": MAX_INFRACOST_ITERATIONS,
        },
        "actions": actions,
    })

    gate = state["human_gates"]["gate_2_pre_deployops"]
    gate["approved"] = approval.get("approved", False)
    gate["comment"] = approval.get("comment", "")
    gate["approved_at"] = datetime.now(timezone.utc).isoformat()
    gate["approved_by"] = approval.get("approved_by", "unknown")

    if approval.get("request_regeneration"):
        # User wants the InfraCost artifacts regenerated from a free-form prompt.
        feedback = approval.get("comment", "").strip()
        gate["requested_changes"] = feedback
        state["infracost_feedback"] = feedback
        logger.info(
            f"[{state['job_id']}] Gate 2 REQUESTED CHANGES "
            f"(iteration {iterations_done + 1}/{MAX_INFRACOST_ITERATIONS}): {feedback}"
        )
    elif not gate["approved"]:
        logger.warning(f"[{state['job_id']}] Gate 2 REJECTED. Workflow halted.")
        # BUGFIX v1.0.4: was "failed" - now "rejected" for consistency with
        # gate 1, so monitoring can distinguish a human "no" from a real
        # technical failure.
        state["status"] = "rejected"
        state["error_log"].append({
            "node": "human_gate_2",
            "attempt": 1,
            "max_attempts": 1,
            "message": f"Human gate 2 rejected: {approval.get('comment', 'No comment')}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stack_trace": None,
            "resolved": False,
        })
    else:
        logger.info(f"[{state['job_id']}] Gate 2 APPROVED. Proceeding to DeployOps.")

    return state
