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

from typing import Final

DOCKER_IMAGE_NAME: Final[str] = "devguard-app"

# ECS
ECS_CLUSTER_NAME: Final[str] = "devguard-cluster"
ECS_SERVICE_NAME: Final[str] = "app-service"
ECS_HEALTH_CHECK_PORT: Final[int] = 8080
ECS_HEALTH_CHECK_PATH: Final[str] = "/health"
ECS_TASK_EXECUTION_ROLE_NAME: Final[str] = "devguard-task-execution-role"

# Lambda
LAMBDA_FUNCTION_NAME: Final[str] = "app-handler"
LAMBDA_HANDLER: Final[str] = "handler.main"
LAMBDA_RUNTIME: Final[str] = "python3.12"
LAMBDA_TIMEOUT_SECONDS: Final[int] = 30

# EC2
EC2_AMI_ID: Final[str] = "ami-0000000000000000"
EC2_KEY_PAIR_NAME: Final[str] = "devguard-key"
EC2_INSTANCE_COUNT: Final[int] = 1
EC2_INSTANCE_NAME: Final[str] = "devguard-app"
