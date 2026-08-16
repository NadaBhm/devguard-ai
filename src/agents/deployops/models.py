"""
Pydantic models for DeployOps  
"""
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field
from enum import Enum


class DeploymentStrategy(str, Enum):
    ROLLING = "rolling"
    BLUE_GREEN = "blue_green"
    CANARY = "canary"


class HealthCheckConfig(BaseModel):
    path: str = "/health"
    port: int = 80
    interval_seconds: int = 30
    timeout_seconds: int = 10
    healthy_threshold: int = 2
    unhealthy_threshold: int = 3


class DockerImageConfig(BaseModel):
    name: str
    dockerfile: str
    context: str = "."
    tag: str = "latest"
    build_args: Dict[str, str] = Field(default_factory=dict)
    platform: str = "linux/amd64"


class TerraformArtifacts(BaseModel):
    files: Dict[str, str]  # filename -> content
    variables: Dict[str, Any] = Field(default_factory=dict)


class Artifacts(BaseModel):
    terraform: TerraformArtifacts
    docker_images: List[DockerImageConfig] = Field(default_factory=list)


class AWSConfig(BaseModel):
    region: str = "us-east-1"
    assume_role_arn: Optional[str] = None
    target_account_id: Optional[str] = None
    ecs_cluster: Optional[str] = None
    service_name: Optional[str] = None
    bucket_name: Optional[str] = None


class DeploymentConfig(BaseModel):
    strategy: DeploymentStrategy = DeploymentStrategy.ROLLING
    health_check_path: str = "/health"
    health_check_port: int = 80
    min_healthy_percent: int = 50
    max_percent: int = 200
    auto_rollback: bool = True
    rollback_on_alarm: bool = True
    timeout_minutes: int = 15


class Approval(BaseModel):
    deploy_approved: bool = False
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None


class DeployPayload(BaseModel):
    job_id: str
    artifacts: Artifacts
    aws_config: AWSConfig = Field(default_factory=AWSConfig)
    deployment_config: DeploymentConfig = Field(default_factory=DeploymentConfig)
    approval: Approval = Field(default_factory=Approval)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    compute_type: str = "ecs"


class RollbackRequest(BaseModel):
    job_id: str
    service_name: str
    cluster: str
    target_deployment: Optional[int] = None
    reason: Optional[str] = None


class PromotionRequest(BaseModel):
    job_id: str
    from_environment: str
    to_environment: str
    service_name: str
    deployment_version: Optional[int] = None