"""Single source of truth for the agent's naming/operational conventions.

Previously mirrored between terraform_generator (module 3) and output_builder
(module 7) — drift silently produced JSON metadata disagreeing with the shipped
Terraform. Both import from here. Identity/default conventions only, NOT
architecture decisions.
"""

from __future__ import annotations

import os
from typing import Final

DOCKER_IMAGE_NAME: Final[str] = "devguard-app"

# Whole-repo digestion (Gate-2 regens): caps on how much of the repo the LLM reads
# and how long a single digest call may take.
REPO_CHUNK_BYTES: Final[int] = int(os.getenv("REPO_CHUNK_BYTES", "30000"))
REPO_MAX_BYTES: Final[int] = int(os.getenv("REPO_MAX_BYTES", "4000000"))
REPO_MAX_CHUNKS: Final[int] = int(os.getenv("REPO_MAX_CHUNKS", "40"))
REPO_MAX_FILE_BYTES: Final[int] = int(os.getenv("REPO_MAX_FILE_BYTES", "524288"))
REPO_DIGEST_TIMEOUT_SECONDS: Final[float] = float(
    os.getenv("REPO_DIGEST_TIMEOUT_SECONDS", "60")
)

# Refiner retries: free OpenRouter tiers intermittently return 200s lacking `choices`
# (or fence-wrapped JSON), so re-ask a few times before keeping the generated files.
REFINER_MAX_ATTEMPTS: Final[int] = int(os.getenv("REFINER_MAX_ATTEMPTS", "3"))
REFINER_RETRY_DELAY_SECONDS: Final[float] = float(
    os.getenv("REFINER_RETRY_DELAY_SECONDS", "1.0")
)

def unique_resource_name(base: str, job_id: str) -> str:
    """Suffix a base name with a short deterministic slice of ``job_id``: concurrent
    deployments collided on fixed names in practice (incl. derived ALB/target-group/
    log-group names); 8 hex chars fits AWS name limits. Callers must pass same job_id."""
    return f"{base}-{job_id[:8]}"
# ECS
ECS_CLUSTER_NAME: Final[str] = "devguard-cluster"
ECS_SERVICE_NAME: Final[str] = "app-service"
ECS_HEALTH_CHECK_PORT: Final[int] = 8080
# "/" is universally served; "/health" often is not.
ECS_HEALTH_CHECK_PATH: Final[str] = "/"
ECS_TASK_EXECUTION_ROLE_NAME: Final[str] = "devguard-task-execution-role"

LAMBDA_FUNCTION_NAME: Final[str] = "app-handler"
LAMBDA_HANDLER: Final[str] = "handler.main"
LAMBDA_RUNTIME: Final[str] = "python3.12"
LAMBDA_TIMEOUT_SECONDS: Final[int] = 30

EC2_AMI_ID: Final[str] = "ami-0000000000000000"
# Optional SSH key pair (DEVGUARD_KEY_PAIR_NAME); default empty — user_data suffices,
# and a hardcoded missing pair broke applies with InvalidKeyPair.NotFound.
EC2_KEY_PAIR_NAME: Final[str] = os.getenv("DEVGUARD_KEY_PAIR_NAME", "")
EC2_INSTANCE_COUNT: Final[int] = 1
EC2_INSTANCE_NAME: Final[str] = "devguard-app"
EC2_HEALTH_CHECK_PORT: Final[int] = 8080
EC2_HEALTH_CHECK_PATH: Final[str] = "/"
EC2_INSTANCE_PORT: Final[int] = 8080
EC2_SSH_CIDR: Final[str] = "0.0.0.0/0"

S3_BUCKET_PREFIX: Final[str] = "devguard-static"
S3_INDEX_DOCUMENT: Final[str] = "index.html"
S3_ERROR_DOCUMENT: Final[str] = "404.html"
S3_HEALTH_CHECK_PATH: Final[str] = "/"
