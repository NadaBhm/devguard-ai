"""Step 8 of the InfraCost pipeline: local/CLI/test-only approval state.

A small pending -> approved | rejected state machine. This is NOT the
approval authority when this agent runs inside the shared orchestrator
(``src/subgroup2/orchestrator/graph.py``) — there, the orchestrator's own
``interrupt()``-based human gates (``gate_1_pre_infracost``,
``gate_2_pre_deployops``) are the sole source of truth, and this class is
simply never consulted on that path. It exists only for running this agent
standalone (its own API, a CLI, or tests), where no orchestrator gate
exists to ask instead.

(Decided 2026-07-27, verified against the orchestrator's actual code before
building this — see project memory — rather than assumed.)
"""

from __future__ import annotations

from models.output_schema import Approval, ApprovalStatus


class InvalidApprovalTransitionError(Exception):
    """An approve/reject action was attempted from a non-``pending`` state."""

    def __init__(self, job_id: str, current_status: ApprovalStatus, attempted_action: str) -> None:
        self.job_id = job_id
        self.current_status = current_status
        self.attempted_action = attempted_action
        super().__init__(
            f"job_id={job_id}: cannot {attempted_action} — current status is "
            f"'{current_status}', not 'pending'"
        )


class ApprovalManager:
    """Tracks one job's approval state through pending -> approved|rejected.

    Once a decision is made (either way), it is final — approving twice,
    or rejecting an already-approved (or already-rejected) job, always
    raises rather than silently overwriting the earlier decision.
    """

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self._status: ApprovalStatus = "pending"
        self._approved_by: str | None = None

    @property
    def status(self) -> ApprovalStatus:
        return self._status

    @property
    def approved_by(self) -> str | None:
        return self._approved_by

    def approve(self, approved_by: str) -> None:
        """Move ``pending`` -> ``approved``.

        Raises:
            InvalidApprovalTransitionError: the job isn't ``pending``
                (already approved, or already rejected).
        """
        if self._status != "pending":
            raise InvalidApprovalTransitionError(self.job_id, self._status, "approve")
        self._status = "approved"
        self._approved_by = approved_by

    def reject(self) -> None:
        """Move ``pending`` -> ``rejected``.

        Raises:
            InvalidApprovalTransitionError: the job isn't ``pending``
                (already approved, or already rejected).
        """
        if self._status != "pending":
            raise InvalidApprovalTransitionError(self.job_id, self._status, "reject")
        self._status = "rejected"

    def to_approval(self) -> Approval:
        """The current state, in the shape module 7's output contract expects."""
        return Approval(status=self._status, approved_by=self._approved_by)
