from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import UUID
from pydantic import BaseModel, EmailStr, Field
from enum import Enum


# Enums
class UserRole(str, Enum):
    MEMBER = "member"
    ADMIN = "admin"
    OWNER = "owner"


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ARCHIVED = "archived"


class AnalysisRunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentName(str, Enum):
    CODESEC = "codesec"
    INFRACOST = "infracost"
    DEPLOYOPS = "deployops"


class AgentTaskStatus(str, Enum):
    PENDING = "pending"
    STARTED = "started"
    SUCCESS = "success"
    FAILURE = "failure"
    RETRYING = "retrying"


class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ScannerType(str, Enum):
    SEMGREP = "semgrep"
    GITLEAKS = "gitleaks"
    TRIVY = "trivy"
    BANDIT = "bandit"


class Environment(str, Enum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class DeploymentStatus(str, Enum):
    PENDING = "pending"
    APPLYING = "applying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class NotificationType(str, Enum):
    FINDING = "finding"
    DEPLOYMENT = "deployment"
    COST_ALERT = "cost_alert"
    SECURITY_BREACH = "security_breach"


class NotificationSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class CostAlertType(str, Enum):
    BUDGET_EXCEEDED = "budget_exceeded"
    COST_SPIKE = "cost_spike"
    UNUSUAL_RESOURCE = "unusual_resource"


class RAGSourceType(str, Enum):
    GUIDE = "guide"
    PATTERN = "pattern"
    BEST_PRACTICE = "best_practice"
    SECURITY_HARDENING = "security_hardening"
    COST_OPTIMIZATION = "cost_optimization"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# User schemas
class UserBase(BaseModel):
    email: EmailStr
    first_name: str
    last_name: str
    role: UserRole = UserRole.MEMBER


class UserCreate(UserBase):
    password: str


class UserUpdate(BaseModel):
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_verified: Optional[bool] = None
    role: Optional[UserRole] = None


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    email: Optional[str] = None


class User(UserBase):
    id: UUID
    is_verified: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Project schemas
class ProjectBase(BaseModel):
    repo_name: str
    github_url: str
    default_branch: str = "main"


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    repo_name: Optional[str] = None
    github_url: Optional[str] = None
    default_branch: Optional[str] = None
    is_active: Optional[bool] = None


class Project(ProjectBase):
    id: UUID
    user_id: UUID
    connected_at: datetime
    last_analyzed_at: Optional[datetime] = None
    updated_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


# Analysis Run schemas
class AnalysisRunBase(BaseModel):
    commit_sha: str
    commit_message: Optional[str] = None


class AnalysisRunCreate(AnalysisRunBase):
    project_id: UUID


class AnalysisRunUpdate(BaseModel):
    status: Optional[AnalysisRunStatus] = None
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


class AnalysisRun(AnalysisRunBase):
    id: UUID
    project_id: UUID
    status: AnalysisRunStatus
    triggered_by: UUID
    started_at: datetime
    completed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


# Agent Task schemas
class AgentTaskBase(BaseModel):
    agent_name: AgentName
    celery_task_id: UUID


class AgentTaskCreate(AgentTaskBase):
    run_id: UUID


class AgentTaskUpdate(BaseModel):
    status: Optional[AgentTaskStatus] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: Optional[int] = None
    raw_result: Optional[Dict[str, Any]] = None


class AgentTask(AgentTaskBase):
    id: UUID
    run_id: UUID
    status: AgentTaskStatus
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error_message: Optional[str] = None
    retry_count: int
    raw_result: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True


# CodeSec Finding schemas
class CodeSecFindingBase(BaseModel):
    scanner: ScannerType
    severity: Severity
    file_path: str
    line_number: Optional[int] = None
    rule_id: str
    rule_title: str
    description: str
    remediation_hint: Optional[str] = None
    raw_json: Optional[Dict[str, Any]] = None


class CodeSecFindingCreate(CodeSecFindingBase):
    run_id: UUID


class CodeSecFinding(CodeSecFindingBase):
    id: UUID
    run_id: UUID
    created_at: datetime

    class Config:
        from_attributes = True


# Infracost Estimate schemas
class InfracostEstimateBase(BaseModel):
    resource_type: str
    resource_name: str
    monthly_cost_usd: float
    annual_cost_usd: float
    usage_assumptions: Optional[Dict[str, Any]] = None
    cost_drivers: Optional[Dict[str, Any]] = None
    terraform_plan_ref: Optional[str] = None
    confidence_level: Optional[ConfidenceLevel] = None


class InfracostEstimateCreate(InfracostEstimateBase):
    run_id: UUID


class InfracostEstimate(InfracostEstimateBase):
    id: UUID
    run_id: UUID
    created_at: datetime

    class Config:
        from_attributes = True


# Terraform Artifact schemas
class TerraformArtifactBase(BaseModel):
    artifact_type: str
    file_path: str
    content: str
    checksum: Optional[str] = None


class TerraformArtifactCreate(TerraformArtifactBase):
    run_id: UUID


class TerraformArtifact(TerraformArtifactBase):
    id: UUID
    run_id: UUID
    created_at: datetime

    class Config:
        from_attributes = True


# Deployment schemas
class DeploymentBase(BaseModel):
    environment: Environment
    aws_region: str
    terraform_version: Optional[str] = None
    terraform_state_id: Optional[str] = None
    infrastructure_json: Optional[Dict[str, Any]] = None
    cost_total_monthly: Optional[float] = None


class DeploymentCreate(DeploymentBase):
    run_id: UUID


class DeploymentUpdate(BaseModel):
    status: Optional[DeploymentStatus] = None
    applied_at: Optional[datetime] = None
    rollback_reason: Optional[str] = None
    infrastructure_json: Optional[Dict[str, Any]] = None
    cost_total_monthly: Optional[float] = None


class Deployment(DeploymentBase):
    id: UUID
    run_id: UUID
    status: DeploymentStatus
    applied_at: Optional[datetime] = None
    rollback_reason: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# Notification schemas
class NotificationBase(BaseModel):
    type: NotificationType
    severity: NotificationSeverity
    title: str
    body: str
    related_finding_id: Optional[UUID] = None


class NotificationCreate(NotificationBase):
    user_id: UUID
    run_id: UUID


class NotificationUpdate(BaseModel):
    is_read: Optional[bool] = None
    read_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None


class Notification(NotificationBase):
    id: UUID
    user_id: UUID
    run_id: UUID
    is_read: bool
    created_at: datetime
    read_at: Optional[datetime] = None
    dismissed_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# Cost Alert schemas
class CostAlertBase(BaseModel):
    alert_type: CostAlertType
    threshold_usd: float
    actual_cost_usd: float
    severity: NotificationSeverity


class CostAlertCreate(CostAlertBase):
    run_id: UUID
    project_id: UUID
    user_id: UUID


class CostAlertUpdate(BaseModel):
    is_resolved: Optional[bool] = None
    resolved_at: Optional[datetime] = None


class CostAlert(CostAlertBase):
    id: UUID
    run_id: UUID
    project_id: UUID
    user_id: UUID
    is_resolved: bool
    created_at: datetime
    resolved_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# RAG Document schemas
class RAGDocumentBase(BaseModel):
    source_path: str
    source_type: RAGSourceType
    title: str
    content_preview: Optional[str] = None
    qdrant_point_id: str
    embedding_model: str
    tags: Optional[Dict[str, Any]] = None


class RAGDocumentCreate(RAGDocumentBase):
    pass


class RAGDocumentUpdate(BaseModel):
    source_type: Optional[RAGSourceType] = None
    title: Optional[str] = None
    content_preview: Optional[str] = None
    tags: Optional[Dict[str, Any]] = None


class RAGDocument(RAGDocumentBase):
    id: UUID
    indexed_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# Response schemas with relationships
class AnalysisRunWithDetails(AnalysisRun):
    project: Optional[Project] = None
    agent_tasks: List[AgentTask] = []
    codesec_findings: List[CodeSecFinding] = []
    infracost_estimates: List[InfracostEstimate] = []
    deployments: List[Deployment] = []
    notifications: List[Notification] = []
    cost_alerts: List[CostAlert] = []


class ProjectWithRuns(Project):
    analysis_runs: List[AnalysisRun] = []


class UserWithProjects(User):
    projects: List[Project] = []