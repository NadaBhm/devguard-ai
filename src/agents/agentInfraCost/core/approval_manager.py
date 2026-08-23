"""Step 8: local/CLI/test-only approval state (pending -> approved | rejected).

NOT the authority inside the shared orchestrator — its interrupt()-based human
gates are the sole source of truth there and this class is never consulted;
standalone runs (own API/CLI/tests) only. Verified against orchestrator code.
"""

from __future__ import annotations

from models.output_schema import Approval, ApprovalStatus


class InvalidApprovalTransitionError(Exception):
    def __init__(self, job_id: str, current_status: ApprovalStatus, attempted_action: str) -> None:
        self.job_id = job_id
        self.current_status = current_status
        self.attempted_action = attempted_action
        super().__init__(
            f"job_id={job_id}: cannot {attempted_action} — current status is "
            f"'{current_status}', not 'pending'"
        )


class ApprovalManager:
    """Tracks one job through pending -> approved|rejected; a decision is final —
    double approve/reject raises rather than silently overwriting.
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
        if self._status != "pending":
            raise InvalidApprovalTransitionError(self.job_id, self._status, "approve")
        self._status = "approved"
        self._approved_by = approved_by

    def reject(self) -> None:
        if self._status != "pending":
            raise InvalidApprovalTransitionError(self.job_id, self._status, "reject")
        self._status = "rejected"

    def to_approval(self) -> Approval:
        """The current state, in the shape module 7's output contract expects."""
        return Approval(status=self._status, approved_by=self._approved_by)
