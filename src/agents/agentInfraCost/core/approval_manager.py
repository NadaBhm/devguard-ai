"""Module 8: manages the pending -> approved/rejected state transition.

The wire contract's Approval.status enum (pending/approved/rejected) is a
one-way state machine from this module's perspective: a job can be approved
or rejected exactly once, from "pending", and never transitions again after
that. Approving an already-decided job (approved or rejected) is a
programming/workflow error caught here, never silently overwritten.

Transitions are pure: each function returns a new ApprovalRecord rather than
mutating the one it was given.
"""

from __future__ import annotations

import logging

from ..models.exceptions import InfraCostAgentError
from ..models.internal_models import ApprovalRecord

logger = logging.getLogger(__name__)


class InvalidApprovalTransitionError(InfraCostAgentError):
    """Raised when an approve/reject is attempted from a non-"pending" state."""

    def __init__(self, job_id: str, current_status: str, attempted_transition: str) -> None:
        self.job_id = job_id
        self.current_status = current_status
        self.attempted_transition = attempted_transition
        super().__init__(
            f"Cannot {attempted_transition} job '{job_id}': it is already "
            f"'{current_status}', not 'pending'"
        )


def create_approval_record(job_id: str) -> ApprovalRecord:
    """Creates a new approval record in the initial 'pending' state."""
    return ApprovalRecord(job_id=job_id, status="pending", approved_by=None)


def approve(record: ApprovalRecord, *, approved_by: str) -> ApprovalRecord:
    """Transitions `record` from 'pending' to 'approved'.

    :raises InvalidApprovalTransitionError: if `record.status` is not
        'pending' (already approved or already rejected)
    """
    if record.status != "pending":
        raise InvalidApprovalTransitionError(record.job_id, record.status, "approve")
    logger.info("Job '%s' approved by %s", record.job_id, approved_by)
    return ApprovalRecord(job_id=record.job_id, status="approved", approved_by=approved_by)


def reject(record: ApprovalRecord) -> ApprovalRecord:
    """Transitions `record` from 'pending' to 'rejected'.

    :raises InvalidApprovalTransitionError: if `record.status` is not
        'pending' (already approved or already rejected)
    """
    if record.status != "pending":
        raise InvalidApprovalTransitionError(record.job_id, record.status, "reject")
    logger.info("Job '%s' rejected", record.job_id)
    return ApprovalRecord(job_id=record.job_id, status="rejected", approved_by=None)
