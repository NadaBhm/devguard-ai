from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    Column, String, Boolean, DateTime, Text, Float, Integer, ForeignKey, Enum, CheckConstraint, Index, JSON
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID, JSONB
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship

Base = declarative_base()


class UserRole(str, PyEnum):
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"


class ProjectStatus(str, PyEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class AnalysisRunStatus(str, PyEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentName(str, PyEnum):
    CODESEC = "codesec"
    INFRACOST = "infracost"
    DEPLOYOPS = "deployops"


class AgentTaskStatus(str, PyEnum):
    PENDING = "pending"
    STARTED = "started"
    SUCCESS = "success"
    FAILURE = "failure"
    RETRYING = "retrying"


class Severity(str, PyEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ScannerType(str, PyEnum):
    SEMGREP = "semgrep"
    GITLEAKS = "gitleaks"
    TRIVY = "trivy"
    BANDIT = "bandit"


class Environment(str, PyEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class DeploymentStatus(str, PyEnum):
    PENDING = "pending"
    APPLYING = "applying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class NotificationType(str, PyEnum):
    FINDING = "finding"
    DEPLOYMENT = "deployment"
    COST_ALERT = "cost_alert"
    SECURITY_BREACH = "security_breach"


class NotificationSeverity(str, PyEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class CostAlertType(str, PyEnum):
    BUDGET_EXCEEDED = "budget_exceeded"
    COST_SPIKE = "cost_spike"
    UNUSUAL_RESOURCE = "unusual_resource"


class RAGSourceType(str, PyEnum):
    GUIDE = "guide"
    PATTERN = "pattern"
    BEST_PRACTICE = "best_practice"
    SECURITY_HARDENING = "security_hardening"
    COST_OPTIMIZATION = "cost_optimization"


class ConfidenceLevel(str, PyEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class User(Base):
    __tablename__ = "users"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    password = Column(String, nullable=False)  # hashed
    is_verified = Column(Boolean, nullable=False, default=False)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    role = Column(String(50), nullable=False, default=UserRole.MEMBER.value)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    deleted_at = Column(DateTime, nullable=True)

    # Relationships
    projects = relationship("Project", back_populates="owner", cascade="all, delete-orphan")
    triggered_runs = relationship("AnalysisRun", back_populates="triggered_by_user", foreign_keys="AnalysisRun.triggered_by")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    cost_alerts = relationship("CostAlert", back_populates="user", cascade="all, delete-orphan")


class Project(Base):
    __tablename__ = "projects"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    repo_name = Column(String, nullable=False)
    github_url = Column(String, unique=True, nullable=False)
    default_branch = Column(String, nullable=False, default="main")
    connected_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_analyzed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    is_active = Column(Boolean, nullable=False, default=True)

    # Relationships
    owner = relationship("User", back_populates="projects")
    analysis_runs = relationship("AnalysisRun", back_populates="project", cascade="all, delete-orphan")
    cost_alerts = relationship("CostAlert", back_populates="project", cascade="all, delete-orphan")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id = Column(PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    commit_sha = Column(String, nullable=False)
    commit_message = Column(Text, nullable=True)
    status = Column(String(50), nullable=False, default=AnalysisRunStatus.QUEUED.value)
    triggered_by = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    run_metadata = Column("run_metadata", JSONB, nullable=True)

    # Relationships
    project = relationship("Project", back_populates="analysis_runs")
    triggered_by_user = relationship("User", back_populates="triggered_runs", foreign_keys=[triggered_by])
    agent_tasks = relationship("AgentTask", back_populates="run", cascade="all, delete-orphan")
    codesec_findings = relationship("CodeSecFinding", back_populates="run", cascade="all, delete-orphan")
    infracost_estimates = relationship("InfracostEstimate", back_populates="run", cascade="all, delete-orphan")
    terraform_artifacts = relationship("TerraformArtifact", back_populates="run", cascade="all, delete-orphan")
    deployments = relationship("Deployment", back_populates="run", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="run", cascade="all, delete-orphan")
    cost_alerts = relationship("CostAlert", back_populates="run", cascade="all, delete-orphan")


class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id = Column(PGUUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    agent_name = Column(String(50), nullable=False)
    celery_task_id = Column(PGUUID(as_uuid=True), unique=True, nullable=False)
    status = Column(String(50), nullable=False, default=AgentTaskStatus.PENDING.value)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    raw_result = Column(JSONB, nullable=True)

    # Relationships
    run = relationship("AnalysisRun", back_populates="agent_tasks")

    __table_args__ = (
        CheckConstraint(
            "agent_name IN ('codesec', 'infracost', 'deployops')",
            name="ck_agent_tasks_agent_name"
        ),
        CheckConstraint(
            "status IN ('pending', 'started', 'success', 'failure', 'retrying')",
            name="ck_agent_tasks_status"
        ),
    )


class CodeSecFinding(Base):
    __tablename__ = "codesec_findings"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id = Column(PGUUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    scanner = Column(String(50), nullable=False)
    severity = Column(String(50), nullable=False)
    file_path = Column(Text, nullable=False)
    line_number = Column(Integer, nullable=True)
    rule_id = Column(String, nullable=False)
    rule_title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    remediation_hint = Column(Text, nullable=True)
    raw_json = Column(JSONB, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    run = relationship("AnalysisRun", back_populates="codesec_findings")

    __table_args__ = (
        CheckConstraint(
            "scanner IN ('semgrep', 'gitleaks', 'trivy', 'bandit')",
            name="ck_codesec_findings_scanner"
        ),
        CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_codesec_findings_severity"
        ),
        Index("idx_codesec_findings_run_id", "run_id"),
        Index("idx_codesec_findings_severity", "severity"),
    )


class InfracostEstimate(Base):
    __tablename__ = "infracost_estimates"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id = Column(PGUUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    resource_type = Column(String, nullable=False)
    resource_name = Column(String, nullable=False)
    monthly_cost_usd = Column(Float, nullable=False)
    annual_cost_usd = Column(Float, nullable=False)
    usage_assumptions = Column(JSONB, nullable=True)
    cost_drivers = Column(JSONB, nullable=True)
    confidence_level = Column(String(50), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    run = relationship("AnalysisRun", back_populates="infracost_estimates")

    __table_args__ = (
        CheckConstraint(
            "confidence_level IN ('high', 'medium', 'low')",
            name="ck_infracost_estimates_confidence"
        ),
        Index("idx_infracost_estimates_run_id", "run_id"),
    )


class TerraformArtifact(Base):
    __tablename__ = "terraform_artifacts"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id = Column(PGUUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    artifact_type = Column(String(50), nullable=False)
    file_path = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    checksum = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    run = relationship("AnalysisRun", back_populates="terraform_artifacts")

    __table_args__ = (
        CheckConstraint(
            "artifact_type IN ('terraform', 'dockerfile', 'docker-compose', 'cloudformation', 'helm', 'kubernetes', 'ansible', 'pulumi', 'bicep')",
            name="ck_terraform_artifacts_artifact_type"
        ),
        Index("idx_terraform_artifacts_run_id", "run_id"),
    )


class Deployment(Base):
    __tablename__ = "deployments"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id = Column(PGUUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    environment = Column(String(50), nullable=False)
    aws_region = Column(String, nullable=False)
    terraform_version = Column(String, nullable=True)
    terraform_state_id = Column(String, nullable=True)
    status = Column(String(50), nullable=False, default=DeploymentStatus.PENDING.value)
    applied_at = Column(DateTime, nullable=True)
    rollback_reason = Column(Text, nullable=True)
    infrastructure_json = Column(JSONB, nullable=True)
    cost_total_monthly = Column(Float, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    run = relationship("AnalysisRun", back_populates="deployments")

    __table_args__ = (
        CheckConstraint(
            "environment IN ('dev', 'staging', 'prod')",
            name="ck_deployments_environment"
        ),
        CheckConstraint(
            "status IN ('pending', 'applying', 'succeeded', 'failed', 'rolled_back')",
            name="ck_deployments_status"
        ),
        Index("idx_deployments_run_id", "run_id"),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(PGUUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    type = Column(String(50), nullable=False)
    severity = Column(String(50), nullable=False)
    title = Column(Text, nullable=False)
    body = Column(Text, nullable=False)
    related_finding_id = Column(PGUUID(as_uuid=True), ForeignKey("codesec_findings.id", ondelete="SET NULL"), nullable=True)
    is_read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    read_at = Column(DateTime, nullable=True)
    dismissed_at = Column(DateTime, nullable=True)

    # Relationships
    user = relationship("User", back_populates="notifications")
    run = relationship("AnalysisRun", back_populates="notifications")
    related_finding = relationship("CodeSecFinding")

    __table_args__ = (
        CheckConstraint(
            "type IN ('finding', 'deployment', 'cost_alert', 'security_breach')",
            name="ck_notifications_type"
        ),
        CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="ck_notifications_severity"
        ),
        Index("idx_notifications_user_id", "user_id"),
        Index("idx_notifications_run_id", "run_id"),
    )


class CostAlert(Base):
    __tablename__ = "cost_alerts"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    run_id = Column(PGUUID(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id = Column(PGUUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    alert_type = Column(String(50), nullable=False)
    threshold_usd = Column(Float, nullable=False)
    actual_cost_usd = Column(Float, nullable=False)
    severity = Column(String(50), nullable=False)
    is_resolved = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    resolved_at = Column(DateTime, nullable=True)

    # Relationships
    run = relationship("AnalysisRun", back_populates="cost_alerts")
    project = relationship("Project", back_populates="cost_alerts")
    user = relationship("User", back_populates="cost_alerts")

    __table_args__ = (
        CheckConstraint(
            "alert_type IN ('budget_exceeded', 'cost_spike', 'unusual_resource')",
            name="ck_cost_alerts_alert_type"
        ),
        CheckConstraint(
            "severity IN ('warning', 'critical')",
            name="ck_cost_alerts_severity"
        ),
        Index("idx_cost_alerts_project_id", "project_id"),
        Index("idx_cost_alerts_run_id", "run_id"),
    )


class RAGDocument(Base):
    __tablename__ = "rag_documents"

    id = Column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_path = Column(String, unique=True, nullable=False)
    source_type = Column(String(50), nullable=False)
    title = Column(String, nullable=False)
    content_preview = Column(Text, nullable=True)
    qdrant_point_id = Column(String, unique=True, nullable=False)
    embedding_model = Column(String, nullable=False)
    tags = Column(JSONB, nullable=True)
    indexed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        CheckConstraint(
            "source_type IN ('guide', 'pattern', 'best_practice', 'security_hardening', 'cost_optimization')",
            name="ck_rag_documents_source_type"
        ),
    )