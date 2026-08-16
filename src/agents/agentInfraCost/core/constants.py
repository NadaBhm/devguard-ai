"""Single source of truth for the InfraCost agent's naming/operational
conventions.

Previously these constants were mirrored between ``core.terraform_generator``
(module 3) and ``core.output_builder`` (module 7) — two independent copies of
the same ports, names, runtimes and AMIs. Any drift between them silently
produced artifacts whose metadata (JSON output) disagreed with the Terraform
they shipped. Both modules now import from here instead.

These are identity/default conventions, NOT architecture decisions — the
architecture choice (compute_type, sizing) always comes from the
``DecisionResult``.
"""

from __future__ import annotations

import os
from typing import Final

DOCKER_IMAGE_NAME: Final[str] = "devguard-app"

# Whole-repo digestion (Gate-2 regeneration): the OpenRouter LLM reads the
# repository in chunks and extracts infra-relevant facts. These cap how much
# of the repo is ever read and how long a single digest call may take.
REPO_CHUNK_BYTES: Final[int] = int(os.getenv("REPO_CHUNK_BYTES", "30000"))
REPO_MAX_BYTES: Final[int] = int(os.getenv("REPO_MAX_BYTES", "4000000"))
REPO_MAX_CHUNKS: Final[int] = int(os.getenv("REPO_MAX_CHUNKS", "40"))
REPO_MAX_FILE_BYTES: Final[int] = int(os.getenv("REPO_MAX_FILE_BYTES", "524288"))
REPO_DIGEST_TIMEOUT_SECONDS: Final[float] = float(
    os.getenv("REPO_DIGEST_TIMEOUT_SECONDS", "60")
)

# Feedback-driven artifact refinement (core.llm_terraform_refiner): the free
# OpenRouter tiers intermittently return a 200 whose body lacks the expected
# `choices` envelope (or wraps the JSON in a markdown fence), so the refiner
# re-asks a few times before giving up and keeping the generated files
# unchanged. Each attempt is a fresh request, so transient provider flakiness
# does not silently drop the user's regeneration request.
REFINER_MAX_ATTEMPTS: Final[int] = int(os.getenv("REFINER_MAX_ATTEMPTS", "3"))
REFINER_RETRY_DELAY_SECONDS: Final[float] = float(
    os.getenv("REFINER_RETRY_DELAY_SECONDS", "1.0")
)

def unique_resource_name(base: str, job_id: str) -> str:
    """Suffix a base AWS resource name with a short, deterministic slice of
    ``job_id`` so concurrent deployments never collide on the same fixed
    name (cluster, service, IAM role, ALB, target group, log group -- the
    ALB/target group/log-group names are all DERIVED from service_name in
    the Terraform template, so suffixing service_name here is enough to
    make those unique too, without touching main.tf.j2).
    Confirmed colliding in practice: two people (or even one person running
    two jobs) hit ELBv2 Target Group / IAM Role / CloudWatch Log Group
    "already exists" on `terraform apply`, because every job used to reuse
    the exact same fixed names. 8 hex chars keeps every AWS name-length
    limit involved (ALB/target group: 32 chars, IAM role: 64 chars) with
    comfortable room to spare.
    Must be called with the SAME job_id from both output_builder.py (what
    DeployOps is told the service is named) and terraform_generator.py
    (what Terraform actually creates) -- same job_id in, same suffix out,
    so the two never drift apart.
    """
    return f"{base}-{job_id[:8]}"
# ECS
ECS_CLUSTER_NAME: Final[str] = "devguard-cluster"
ECS_SERVICE_NAME: Final[str] = "app-service"
ECS_HEALTH_CHECK_PORT: Final[int] = 8080
ECS_HEALTH_CHECK_PATH: Final[str] = "/health"
ECS_TASK_EXECUTION_ROLE_NAME: Final[str] = "devguard-task-execution-role"

LAMBDA_FUNCTION_NAME: Final[str] = "app-handler"
LAMBDA_HANDLER: Final[str] = "handler.main"
LAMBDA_RUNTIME: Final[str] = "python3.12"
LAMBDA_TIMEOUT_SECONDS: Final[int] = 30

EC2_AMI_ID: Final[str] = "ami-0000000000000000"
EC2_KEY_PAIR_NAME: Final[str] = "devguard-key"
EC2_INSTANCE_COUNT: Final[int] = 1
EC2_INSTANCE_NAME: Final[str] = "devguard-app"
EC2_HEALTH_CHECK_PORT: Final[int] = 8080
EC2_HEALTH_CHECK_PATH: Final[str] = "/health"
EC2_INSTANCE_PORT: Final[int] = 8080
EC2_SSH_CIDR: Final[str] = "0.0.0.0/0"

S3_BUCKET_PREFIX: Final[str] = "devguard-static"
S3_INDEX_DOCUMENT: Final[str] = "index.html"
S3_ERROR_DOCUMENT: Final[str] = "404.html"
S3_HEALTH_CHECK_PATH: Final[str] = "/"
