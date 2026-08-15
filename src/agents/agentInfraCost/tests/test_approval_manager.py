import pytest

from core.approval_manager import ApprovalManager, InvalidApprovalTransitionError


def test_new_manager_starts_pending() -> None:
    manager = ApprovalManager(job_id="job-1")
    assert manager.status == "pending"
    assert manager.approved_by is None


def test_approve_from_pending_succeeds() -> None:
    manager = ApprovalManager(job_id="job-1")
    manager.approve(approved_by="alice@example.com")
    assert manager.status == "approved"
    assert manager.approved_by == "alice@example.com"


def test_reject_from_pending_succeeds() -> None:
    manager = ApprovalManager(job_id="job-1")
    manager.reject()
    assert manager.status == "rejected"


def test_to_approval_reflects_current_state() -> None:
    manager = ApprovalManager(job_id="job-1")
    manager.approve(approved_by="bob@example.com")
    approval = manager.to_approval()
    assert approval.status == "approved"
    assert approval.approved_by == "bob@example.com"


def test_two_independent_jobs_do_not_share_state() -> None:
    job_a = ApprovalManager(job_id="job-a")
    job_b = ApprovalManager(job_id="job-b")
    job_a.approve(approved_by="alice@example.com")
    assert job_a.status == "approved"
    assert job_b.status == "pending"


def test_to_approval_before_any_decision_has_no_approver() -> None:
    manager = ApprovalManager(job_id="job-1")
    approval = manager.to_approval()
    assert approval.status == "pending"
    assert approval.approved_by is None


def test_approve_twice_raises() -> None:
    manager = ApprovalManager(job_id="job-1")
    manager.approve(approved_by="alice@example.com")
    with pytest.raises(InvalidApprovalTransitionError) as excinfo:
        manager.approve(approved_by="bob@example.com")
    assert excinfo.value.job_id == "job-1"
    assert excinfo.value.current_status == "approved"


def test_reject_an_already_approved_job_raises() -> None:
    manager = ApprovalManager(job_id="job-1")
    manager.approve(approved_by="alice@example.com")
    with pytest.raises(InvalidApprovalTransitionError):
        manager.reject()
    assert manager.status == "approved"


def test_approve_an_already_rejected_job_raises() -> None:
    manager = ApprovalManager(job_id="job-1")
    manager.reject()
    with pytest.raises(InvalidApprovalTransitionError) as excinfo:
        manager.approve(approved_by="alice@example.com")
    assert excinfo.value.current_status == "rejected"
    assert manager.status == "rejected"
