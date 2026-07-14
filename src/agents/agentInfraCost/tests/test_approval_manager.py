import pytest

from agentInfraCost.core.approval_manager import (
    InvalidApprovalTransitionError,
    approve,
    create_approval_record,
    reject,
)
from agentInfraCost.models.internal_models import ApprovalRecord


class TestCreateApprovalRecord:
    def test_starts_in_pending_state(self) -> None:
        record = create_approval_record("job-1")
        assert record.status == "pending"
        assert record.approved_by is None
        assert record.job_id == "job-1"


class TestApprove:
    def test_nominal_transition_from_pending(self) -> None:
        record = create_approval_record("job-1")
        approved = approve(record, approved_by="user@example.com")
        assert approved.status == "approved"
        assert approved.approved_by == "user@example.com"
        assert approved.job_id == "job-1"

    def test_does_not_mutate_the_original_record(self) -> None:
        record = create_approval_record("job-1")
        approve(record, approved_by="user@example.com")
        assert record.status == "pending"

    def test_cannot_approve_twice(self) -> None:
        record = approve(create_approval_record("job-1"), approved_by="user@example.com")
        with pytest.raises(InvalidApprovalTransitionError) as exc_info:
            approve(record, approved_by="someone-else@example.com")
        assert exc_info.value.current_status == "approved"
        assert exc_info.value.attempted_transition == "approve"

    def test_cannot_approve_a_rejected_job(self) -> None:
        record = reject(create_approval_record("job-1"))
        with pytest.raises(InvalidApprovalTransitionError):
            approve(record, approved_by="user@example.com")


class TestReject:
    def test_nominal_transition_from_pending(self) -> None:
        record = create_approval_record("job-1")
        rejected = reject(record)
        assert rejected.status == "rejected"
        assert rejected.approved_by is None

    def test_cannot_reject_twice(self) -> None:
        record = reject(create_approval_record("job-1"))
        with pytest.raises(InvalidApprovalTransitionError) as exc_info:
            reject(record)
        assert exc_info.value.current_status == "rejected"
        assert exc_info.value.attempted_transition == "reject"

    def test_cannot_reject_an_approved_job(self) -> None:
        record = approve(create_approval_record("job-1"), approved_by="user@example.com")
        with pytest.raises(InvalidApprovalTransitionError):
            reject(record)


class TestErrorMessageContent:
    def test_error_message_names_the_job_and_current_status(self) -> None:
        record = ApprovalRecord(job_id="job-42", status="rejected", approved_by=None)
        with pytest.raises(InvalidApprovalTransitionError, match="job-42"):
            approve(record, approved_by="user@example.com")
