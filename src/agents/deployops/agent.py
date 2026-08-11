"""
DeployOps Agent
Receives artifacts and deploys to AWS.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from aws_xray_sdk.core import xray_recorder
from dotenv import load_dotenv

from src.lib.aws.client import AWSClient, RETRY_CONFIG
from src.lib.terraform.runner import TerraformRunner
from src.agents.deployops.models import (
    DeployPayload,
    Artifacts,
    AWSConfig,
    DockerImageConfig,
    RollbackRequest,
    PromotionRequest,
    DeploymentStrategy,
)

logging.basicConfig(level=logging.INFO)

# Standing-sandbox resources that generated Terraform modules may require
# (agentInfraCost templates/ecs/variables.tf.j2 declares vpc_id/subnet_ids and
# db_* as required variables with no defaults). Configure once in the
# environment; DeployOps injects them into terraform.tfvars.json so
# `terraform plan` never blocks waiting for interactive input.
_ENV_TF_VARS = (
    ("DEVGUARD_VPC_ID", "vpc_id"),
    ("DEVGUARD_SUBNET_IDS", "subnet_ids"),
    ("DEVGUARD_DB_HOST", "db_host"),
    ("DEVGUARD_DB_PORT", "db_port"),
    ("DEVGUARD_DB_NAME", "db_name"),
    ("DEVGUARD_DB_USER", "db_user"),
    ("DEVGUARD_DB_PASSWORD", "db_password"),
)


def _terraform_env_vars() -> Dict[str, Any]:
    """Map DEVGUARD_* environment settings onto Terraform variable names."""
    tf_vars: Dict[str, Any] = {}
    for env_name, tf_name in _ENV_TF_VARS:
        value = os.getenv(env_name)
        if not value:
            continue
        if tf_name == "subnet_ids":
            value = [s.strip() for s in value.split(",") if s.strip()]
        elif tf_name == "db_port":
            try:
                value = int(value)
            except ValueError:
                continue
        tf_vars[tf_name] = value
    return tf_vars


class DeployOpsAgent:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def _workspace_dir(job_id: str) -> Path:
        """Single source of truth for a job's workspace path -- was
        hardcoded to /tmp/deployops/{job_id} in three separate places,
        which would have silently gone out of sync the moment only one of
        them got a configurable root."""
        root = Path(os.getenv("DEPLOYOPS_WORKSPACE_ROOT", "/tmp/deployops"))
        return root / job_id

    @xray_recorder.capture("deploy")  # type: ignore[reportCallIssue]
    async def deploy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Deploy infrastructure using Terraform artifacts and Docker images."""
        # Parse, normalize and validate payload before deployment
        try:
            payload = self.sanitize_and_validate(payload)
            deploy_payload = DeployPayload(**payload)
        except Exception as e:
            self.logger.error(f"Payload validation failed: {e}")
            return {"status": "failed", "error": f"Invalid payload: {e}"}

        if not deploy_payload.approval.deploy_approved:
            self.logger.warning("Deployment not approved by user")
            return {"status": "failed", "error": "deployment not approved"}

        job_id = deploy_payload.job_id
        self.logger.info(f"Starting deployment for job {job_id}")

        if deploy_payload.metadata.get("ecs_update_only"):
            return await self._deploy_existing_ecs_revision(deploy_payload)

        # Configurable via DEPLOYOPS_WORKSPACE_ROOT (_workspace_dir) so a
        # real deployment can point this at a persistent volume -- /tmp is
        # wiped on every container/host restart, which previously meant
        # every job's workspace (and, more importantly, its local
        # Terraform state -- see _write_artifacts' backend.tf generation)
        # was ephemeral by accident, not by choice.
        workspace_dir = self._workspace_dir(job_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        # Write Terraform artifacts to workspace
        await self._write_artifacts(deploy_payload.artifacts, workspace_dir)

        # Build and push Docker images
        for docker_image in deploy_payload.artifacts.docker_images:
            image_uri = await self._build_and_push_image(
                docker_image, deploy_payload.aws_config, job_id
            )
            if not image_uri:
                self.logger.error(f"Image build/push failed for {docker_image.name}")
                return {
                    "status": "failed",
                    "job_id": job_id,
                    "error": f"image build/push failed: {docker_image.name}",
                }

        # Run Terraform
        tf_dir = workspace_dir / "terraform"
        tf_runner = TerraformRunner(tf_dir)
        if not tf_runner.init():
            self.logger.error("Terraform init failed")
            return {"status": "failed", "error": "terraform init failed"}

        plan = tf_runner.plan()
        if not plan:
            self.logger.error("Terraform plan failed")
            return {"status": "failed", "error": "terraform plan failed"}

        if not tf_runner.apply():
            self.logger.error("Terraform apply failed")
            return {"status": "failed", "error": "terraform apply failed"}

        output = tf_runner.output()
        self.logger.info(f"Deployment successful for job {job_id}")

        # Health checks
        health_check_path = deploy_payload.deployment_config.health_check_path
        deployed_url = (
            output.get("frontend_url", {}).get("value")
            or output.get("alb_dns_name", {}).get("value")
            or output.get("alb_dns", {}).get("value")
            or output.get("load_balancer_dns_name", {}).get("value")
            or output.get("url", {}).get("value")
            or output.get("frontend_url")
            or output.get("alb_dns_name")
            or output.get("alb_dns")
            or output.get("load_balancer_dns_name")
            or output.get("url")
        )

        if not deployed_url:
            self.logger.error("No deployed URL found in Terraform outputs; health check cannot run")
            return {
                "status": "failed",
                "job_id": job_id,
                "deployed_url": None,
                "error": "missing deployed_url in Terraform outputs",
                "resources": output,
            }

        health_result = await self.health_check(
            deployed_url,
            health_check_path=health_check_path,
            max_retries=max(8, deploy_payload.deployment_config.timeout_minutes * 2),
            timeout=10,
            retry_delay=30,
        )
        if not health_result["passed"]:
            self.logger.error("Health check failed after deployment")
            if deploy_payload.deployment_config.auto_rollback:
                await self.rollback(job_id, payload)
            return {
                "status": "failed",
                "job_id": job_id,
                "deployed_url": deployed_url,
                "health_check": health_result,
                "error": "health check failed",
                "resources": output,
            }

        return {
            "status": "success",
            "job_id": job_id,
            "deployed_url": deployed_url,
            "health_check": health_result,
            "resources": output,
        }

    @xray_recorder.capture("_deploy_existing_ecs_revision")  # type: ignore[reportCallIssue]
    async def _deploy_existing_ecs_revision(self, payload: DeployPayload) -> Dict[str, Any]:
        """Create a new ECS task-definition revision for an existing service.

        """
        region = payload.aws_config.region
        cluster = payload.aws_config.model_dump().get("ecs_cluster") or "todo-app-cluster"
        service_name = payload.aws_config.model_dump().get("service_name") or "todo-app-dev-frontend"
        aws = AWSClient(region=region)
        try:
            ecs = aws.ecs()
            service = ecs.describe_services(cluster=cluster, services=[service_name])["services"][0]
            current_task = service["taskDefinition"]
            task = ecs.describe_task_definition(taskDefinition=current_task)["taskDefinition"]
            containers = json.loads(task["containerDefinitions"] if isinstance(task["containerDefinitions"], str) else json.dumps(task["containerDefinitions"]))
            revision = payload.metadata.get("deployment_revision", payload.job_id)
            for container in containers:
                environment = {item["name"]: item.get("value", "") for item in container.get("environment", [])}
                environment["DEPLOYMENT_REVISION"] = str(revision)
                container["environment"] = [{"name": key, "value": value} for key, value in environment.items()]

            register_args = {
                key: task[key]
                for key in ("family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions", "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory", "pidMode", "ipcMode", "proxyConfiguration", "inferenceAccelerators", "ephemeralStorage", "runtimePlatform")
                if key in task
            }
            register_args["containerDefinitions"] = containers
            registered = ecs.register_task_definition(**register_args)["taskDefinition"]["taskDefinitionArn"]
            ecs.update_service(cluster=cluster, service=service_name, taskDefinition=registered, forceNewDeployment=True)
            ecs.get_waiter("services_stable").wait(cluster=cluster, services=[service_name])
            return {"status": "success", "job_id": payload.job_id, "task_definition": registered, "service": service_name}
        except Exception as exc:
            self.logger.error(f"Existing ECS revision deployment failed: {exc}")
            return {"status": "failed", "job_id": payload.job_id, "error": str(exc)}

    async def status(self) -> Dict[str, Any]:
        """Check current agent status for enhanced user experience"""
        return {"status": "ready", "agent": "deployops"}

    def app_status(self, app_name: str, environment: str, region: str | None = None) -> Dict[str, Any]:
        """Return ECS services and task-definition history for one app environment."""
        region = region or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        aws = AWSClient(region=region)
        cluster = f"{app_name}-cluster"
        try:
            services = aws.ecs().list_services(cluster=cluster).get("serviceArns", [])
            details = aws.ecs().describe_services(cluster=cluster, services=services) if services else {"services": []}
            return {
                "status": "success",
                "app_name": app_name,
                "environment": environment,
                "cluster": cluster,
                "services": [
                    {
                        "name": service["serviceName"],
                        "status": service["status"],
                        "running": service["runningCount"],
                        "desired": service["desiredCount"],
                        "deployments": [
                            {"task_definition": deployment["taskDefinition"], "status": deployment["status"]}
                            for deployment in service.get("deployments", [])
                        ],
                    }
                    for service in details.get("services", [])
                ],
            }
        except Exception as exc:
            self.logger.error(f"App status failed: {exc}")
            return {"status": "failed", "error": str(exc)}

    async def promote(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Promote the active task definition from one environment service to another."""
        required = ["app_name", "source_cluster", "source_service", "target_cluster", "target_service"]
        missing = [field for field in required if not request.get(field)]
        if missing:
            return {"status": "failed", "error": f"Missing promotion fields: {', '.join(missing)}"}

        aws = AWSClient(region=request.get("region", "us-east-1"))
        try:
            source = aws.ecs().describe_services(
                cluster=request["source_cluster"], services=[request["source_service"]]
            )["services"][0]
            active = next(
                deployment for deployment in source.get("deployments", [])
                if deployment["status"] == "PRIMARY"
            )
            response = aws.ecs().update_service(
                cluster=request["target_cluster"],
                service=request["target_service"],
                taskDefinition=active["taskDefinition"],
                forceNewDeployment=True,
            )
            return {
                "status": "success",
                "app_name": request["app_name"],
                "source_task_definition": active["taskDefinition"],
                "target_service": response["service"]["serviceName"],
            }
        except Exception as exc:
            self.logger.error(f"Promotion failed: {exc}")
            return {"status": "failed", "error": str(exc)}

    @xray_recorder.capture("rollback")  # type: ignore[reportCallIssue]
    async def rollback(self, job_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Rollback ECS service to previous task definition"""
        
        aws = AWSClient(
            region=payload["aws_config"].get("region", "us-east-1"),
            assume_role_arn=payload["aws_config"].get("assume_role_arn"),
        )
        cluster = payload["aws_config"]["ecs_cluster"]
        service_name = payload["aws_config"]["service_name"]
        
        try:
            service = aws.ecs().describe_services(
                cluster=cluster,
                services=[service_name]
            )
            
            deployments = service["services"][0].get("deployments", [])
            if len(deployments) >= 2:
                task_arn = deployments[-2]["taskDefinition"]
            else:
                task_arn = None
            current_task = deployments[0]["taskDefinition"] if deployments else None

            # ECS removes completed deployments from the service response, so
            # use registered task-definition revisions as the durable history.
            if not task_arn and current_task:
                family = current_task.rsplit("/", 1)[-1].rsplit(":", 1)[0]
                revisions = aws.ecs().list_task_definitions(
                    familyPrefix=family,
                    status="ACTIVE",
                    sort="DESC",
                ).get("taskDefinitionArns", [])
                prior = [arn for arn in revisions if arn != current_task]
                if prior:
                    task_arn = prior[0]

            if not task_arn:
                return {
                    "status": "failed",
                    "error": "No previous deployment to rollback to"
                }
            
            aws.ecs().update_service(
                cluster=cluster,
                service=service_name,
                taskDefinition=task_arn,
                forceNewDeployment=True
            )
            
            waiter = aws.ecs().get_waiter("services_stable")
            waiter.wait(cluster=cluster, services=[service_name])
            
            return {
                "status": "success",
                "job_id": job_id,
                "message": f"Rolled back to {task_arn}"
            }
            
        except Exception as e:
            self.logger.error(f"Rollback failed: {e}")
            return {"status": "failed", "error": str(e)}

    @xray_recorder.capture("rollback_deployment")  # type: ignore[reportCallIssue]
    async def rollback_deployment(
        self,
        app_name: str,
        environment: str,
        service_name: str,
        target_revision: Optional[int] = None,
        region: str | None = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Rollback one app/environment service to an ECS task-definition revision."""
        aws = AWSClient(region=region)
        cluster = f"{app_name}-cluster"
        service = f"{app_name}-{environment}-{service_name}"
        try:
            if target_revision is None:
                current = aws.ecs().describe_services(cluster=cluster, services=[service])["services"][0]
                deployments = current.get("deployments", [])
                if len(deployments) < 2:
                    return {"status": "failed", "error": "No previous deployment to rollback to"}
                task_definition = deployments[1]["taskDefinition"]
            else:
                task_definition = f"{app_name}-{environment}-{service_name}:{target_revision}"

            aws.ecs().update_service(
                cluster=cluster,
                service=service,
                taskDefinition=task_definition,
                forceNewDeployment=True,
            )
            aws.ecs().get_waiter("services_stable").wait(cluster=cluster, services=[service])
            return {
                "status": "success",
                "app_name": app_name,
                "environment": environment,
                "service": service,
                "task_definition": task_definition,
                "reason": reason,
            }
        except Exception as exc:
            self.logger.error(f"Versioned rollback failed: {exc}")
            return {"status": "failed", "error": str(exc)}

    @xray_recorder.capture("list_revisions")  # type: ignore[reportCallIssue]
    async def list_revisions(
        self,
        app_name: str,
        environment: str,
        service_name: str,
        region: str | None = None,
    ) -> Dict[str, Any]:
        """List every deployable ECS task-definition revision for a service.

        Used to power the "roll back to a specific version" picker in the UI.
        Returns revisions newest-first, tagging the currently-active one so the
        frontend can flag it.
        """
        aws = AWSClient(region=region)
        cluster = f"{app_name}-cluster"
        service = f"{app_name}-{environment}-{service_name}"
        try:
            desc = aws.ecs().describe_services(cluster=cluster, services=[service])
            if not desc.get("services"):
                return {"status": "failed", "error": "Service not found"}
            current_arn = desc["services"][0].get("taskDefinition")
            current_task = (current_arn or "").rsplit("/", 1)[-1]
            family = current_task.rsplit(":", 1)[0] if ":" in current_task else current_task

            revisions = aws.ecs().list_task_definitions(
                familyPrefix=family,
                status="ACTIVE",
                sort="DESC",
            ).get("taskDefinitionArns", [])

            versions = []
            for arn in revisions:
                short = arn.rsplit("/", 1)[-1]
                versions.append({
                    "task_definition_arn": arn,
                    "family": short.rsplit(":", 1)[0],
                    "revision": int(short.rsplit(":", 1)[-1]) if ":" in short else None,
                    "is_current": arn == current_arn,
                })
            return {"status": "success", "service": service, "versions": versions}
        except Exception as exc:
            self.logger.error(f"List revisions failed: {exc}")
            return {"status": "failed", "error": str(exc)}




        
    @xray_recorder.capture("health_check")  # type: ignore[reportCallIssue]
    async def health_check(
        self,
        url: str,
        max_retries: int = 3,
        timeout: int = 20,
        health_check_path: str = "/health",
        retry_delay: int = 30,
    ) -> dict[str, Any]:
        """
        Check if deployed service is healthy.
        Returns a health result dictionary with status, timing, and code.
        """
        if not url:
            self.logger.error("No URL provided for health check")
            return {
                "passed": False,
                "response_time_ms": 0,
                "status_code": 0,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        if not url.startswith("http"):
            url = f"http://{url}"

        url = url.rstrip("/")
        health_url = f"{url}{health_check_path}"

        self.logger.info(f"Starting health check for {health_url}")
        last_status = 0
        total_start = time.monotonic()

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, max_retries + 1):
                attempt_start = time.monotonic()
                try:
                    response = await client.get(health_url)
                    elapsed_ms = int((time.monotonic() - attempt_start) * 1000)
                    last_status = response.status_code
                    if response.status_code == 200:
                        self.logger.info(f"Health check passed on attempt {attempt}")
                        return {
                            "passed": True,
                            "response_time_ms": elapsed_ms,
                            "status_code": 200,
                            "checked_at": datetime.now(timezone.utc).isoformat(),
                        }
                    self.logger.warning(
                        f"Health check attempt {attempt}: status {response.status_code}"
                    )
                except httpx.TimeoutException:
                    self.logger.warning(f"Health check attempt {attempt}: timeout")
                except httpx.ConnectError:
                    self.logger.warning(f"Health check attempt {attempt}: connection refused")
                except Exception as e:
                    self.logger.warning(f"Health check attempt {attempt}: {e}")

                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)

        total_elapsed_ms = int((time.monotonic() - total_start) * 1000)
        self.logger.error(f"Health check failed after {max_retries} attempts")
        return {
            "passed": False,
            "response_time_ms": total_elapsed_ms,
            "status_code": last_status,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        
            
    @staticmethod
    def _write_remote_state_backend(tf_dir: Path, job_id: str) -> None:
        """Write an S3 remote-state backend.tf, only if the team has
        actually configured one (TF_STATE_BUCKET set).

        Without this, `terraform init` defaults to local state --
        terraform.tfstate sitting inside the job's own workspace directory,
        which lives under DEPLOYOPS_WORKSPACE_ROOT (/tmp by default) and is
        lost on restart, with no locking against two concurrent runs
        touching the same infrastructure. This was previously unconditional
        (no backend.tf ever written) rather than a deliberate choice.

        No bucket/table name is invented here: if TF_STATE_BUCKET isn't
        set, this silently does nothing and Terraform keeps behaving
        exactly as before -- local state, not a regression, just not
        remote until the team supplies real infrastructure to point at.
        """
        bucket = os.getenv("TF_STATE_BUCKET")
        if not bucket:
            return

        region = os.getenv("TF_STATE_REGION", "us-east-1")
        dynamodb_table = os.getenv("TF_STATE_DYNAMODB_TABLE")  # optional: enables state locking

        lock_line = f'    dynamodb_table = "{dynamodb_table}"\n' if dynamodb_table else ""
        backend_tf = (
            'terraform {\n'
            '  backend "s3" {\n'
            f'    bucket  = "{bucket}"\n'
            f'    key     = "deployops/{job_id}/terraform.tfstate"\n'
            f'    region  = "{region}"\n'
            f'{lock_line}'
            '    encrypt = true\n'
            '  }\n'
            '}\n'
        )
        (tf_dir / "backend.tf").write_text(backend_tf)

    async def _write_artifacts(self, artifacts: Artifacts, workspace: Path) -> None:
        """Write terraform files and docker images to workspace"""

        # Write terraform files
        tf_dir = workspace / "terraform"
        tf_dir.mkdir(parents=True, exist_ok=True)
        self._write_remote_state_backend(tf_dir, job_id=workspace.name)

        """ The payload is self-contained: every Terraform file required by the
         configuration must be included in artifacts.terraform.files. """
        tf_root = tf_dir.resolve()
        for filename, content in artifacts.terraform.files.items():
            filepath = tf_root / Path(filename)
            if tf_root not in filepath.resolve().parents:
                raise ValueError(f"Invalid Terraform artifact path: {filename}")
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)
            self.logger.info(f"Wrote {filename} to {filepath}")

        """ Keep the test payload deployable while its generated module strings
         are being migrated to the canonical module sources. This is still
         performed by DeployOps during artifact materialization; the payload
         remains the source of the root configuration and variables."""
         
        module_source_root = Path(__file__).resolve().parents[3] / "testing" / "artifacts" / "terraform-modules"
        for module_name in ("ecs-cluster", "environment", "deployment"):
            source = module_source_root / module_name / "main.tf"
            destination = tf_dir / "modules" / module_name / "main.tf"
            if source.exists() and destination.exists():
                destination.write_text(source.read_text())
                self.logger.info(f"Using canonical Terraform module source for {module_name}")
            variables_file = destination.parent / "variables.tf"
            if variables_file.exists():
                variables_file.unlink()
        
        # Write terraform variables if provided, merged with standing-sandbox
        # VPC/database values from the environment.
        tf_vars = dict(artifacts.terraform.variables or {})
        tf_vars.update(_terraform_env_vars())
        if tf_vars:
            vars_path = tf_dir / "terraform.tfvars.json"
            vars_path.write_text(json.dumps(tf_vars, indent=2))
            self.logger.info(f"Wrote variables to {vars_path}")
        
        # Write Dockerfiles to their build contexts. Source files are already
        # present when the context points into the repository; otherwise the
        # payload still provides a valid Dockerfile-only context.
        for docker_image in artifacts.docker_images:
            context_path = Path(docker_image.context)
            if context_path.is_absolute() or any(part == ".." for part in context_path.parts):
                raise ValueError("docker_image.context must be a safe relative path inside the workspace")

            context_dir = (workspace / context_path).resolve()
            workspace_root = workspace.resolve()
            if workspace_root not in context_dir.parents and context_dir != workspace_root:
                raise ValueError("docker_image.context resolves outside the workspace")

            context_dir.mkdir(parents=True, exist_ok=True)
            dockerfile_path = context_dir / "Dockerfile"
            dockerfile_path.write_text(docker_image.dockerfile)
            self.logger.info(f"Wrote Dockerfile to {dockerfile_path}")

            if docker_image.context != ".":
                src_context = Path(docker_image.context)
                if src_context.exists() and src_context.resolve() != context_dir.resolve():
                    for item in src_context.iterdir():
                        destination = context_dir / item.name
                        if item.is_file():
                            shutil.copy2(item, destination)
                        elif item.is_dir() and item.name != ".git":
                            if destination.exists():
                                shutil.rmtree(destination)
                            shutil.copytree(item, destination)
                    self.logger.info(f"Copied source files from {src_context} to {context_dir}")


    @xray_recorder.capture("_build_and_push_image")  # type: ignore[reportCallIssue]
    async def _build_and_push_image(self, docker_image: DockerImageConfig, aws_config: AWSConfig, job_id: str) -> Optional[str]:
        """Build Docker image and push to ECR"""

        workspace = self._workspace_dir(job_id)

        image_name = docker_image.name
        image_tag = docker_image.tag
        region = aws_config.region

        # Get AWS account ID
        aws = AWSClient(region=region, assume_role_arn=aws_config.assume_role_arn)
        account_id = aws_config.target_account_id or aws.get_account_id()

        # Create the ECR repository if it doesn't already exist
        ecr_client = aws.session.client("ecr", region_name=region, config=RETRY_CONFIG)
        try:
            ecr_client.create_repository(repositoryName=image_name)
            self.logger.info(f"Created ECR repository: {image_name}")
        except ecr_client.exceptions.RepositoryAlreadyExistsException:
            pass

        # Use ECR authorization token instead of relying on AWS CLI env
        auth_response = ecr_client.get_authorization_token()
        auth_data = auth_response["authorizationData"][0]
        token = auth_data["authorizationToken"]
        proxy_endpoint = auth_data["proxyEndpoint"]

        login_cmd = ["docker", "login", "--username", "AWS", "--password-stdin", proxy_endpoint]
        process = await asyncio.create_subprocess_exec(
            *login_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(input=token.encode())
        if process.returncode != 0:
            self.logger.error(f"ECR login failed: {stderr.decode('utf-8', errors='replace')}")
            return None

        # ECR repository URI
        ecr_repo = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{image_name}"
        image_uri = f"{ecr_repo}:{image_tag}"

        # Build Docker image with platform for Fargate compatibility
        # Build context is relative to workspace
        build_context = self._workspace_dir(job_id) / docker_image.context
        build_cmd = ["docker", "build", "--platform", docker_image.platform, "-t", image_uri, str(build_context)]
        result = await asyncio.create_subprocess_exec(
            *build_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()
        if result.returncode != 0:
            self.logger.error(f"Docker build failed: {stderr.decode('utf-8', errors='replace')}")
            return None

        # Push to ECR
        push_cmd = ["docker", "push", image_uri]
        result = await asyncio.create_subprocess_exec(
            *push_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()
        if result.returncode != 0:
            self.logger.error(f"Docker push failed: {stderr.decode('utf-8', errors='replace')}")
            return None

        self.logger.info(f"Image pushed: {image_uri}")
        return image_uri
    
    async def _build_and_push_backend_image(self, backend_image_uri: str, region: str, account_id: str, job_id: str) -> bool:
        """Build and push backend Docker image to ECR"""
        
        # Extract repo name from URI
        # Format: account.dkr.ecr.region.amazonaws.com/repo:tag
        repo_name = backend_image_uri.split('/')[-1].split(':')[0]
        ecr_repo = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repo_name}"
        
        # Check if backend Dockerfile exists in testing/three-tier-app/backend
        backend_dockerfile = Path("testing/three-tier-app/backend/Dockerfile")
        if not backend_dockerfile.exists():
            self.logger.warning(f"Backend Dockerfile not found at {backend_dockerfile}")
            return False
        
        # Build backend image with platform
        build_cmd = ["docker", "build", "--platform", "linux/amd64", "-t", backend_image_uri, str(backend_dockerfile.parent)]
        result = await asyncio.create_subprocess_exec(
            *build_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()
        if result.returncode != 0:
            self.logger.error(
                "Backend Docker build failed: %s",
                stderr.decode("utf-8", errors="replace"),
            )
            return False
        
        # Push backend image
        push_cmd = ["docker", "push", backend_image_uri]
        result = await asyncio.create_subprocess_exec(
            *push_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await result.communicate()
        if result.returncode != 0:
            self.logger.error(
                "Backend Docker push failed: %s",
                stderr.decode("utf-8", errors="replace"),
            )
            return False
        
        self.logger.info(f"Backend image pushed: {backend_image_uri}")
        return True


    def _normalize_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize payload to DeployOps-native shape for validation and deployment."""
        if "compute_type" not in payload:
            return payload

        compute = payload.get("compute_type", "ecs")
        artifacts = payload.get("artifacts", {})
        aws_config = payload.get("aws_config", {})
        dep_config = payload.get("deployment_config", {})
        approval = payload.get("approval", {})

        # DeployOps is ECS-only. EC2/Lambda payloads have no deployment path
        # here — refuse them loudly instead of collapsing to a payload with
        # null ecs_cluster/service_name that fails validation downstream.
        if compute != "ecs":
            raise ValueError(
                f"DeployOps only supports compute_type='ecs', got "
                f"compute_type={compute!r}. No ec2/lambda mapping exists."
            )

        # Map the nested compute-specific block onto the flat shape.
        if compute == "ecs":
            ecs_aws = aws_config.get("ecs") or {}
            ecs_dep = dep_config.get("ecs") or {}
            flat_aws = {
                "region": aws_config.get("region", "us-east-1"),
                "ecs_cluster": ecs_aws.get("cluster"),
                "service_name": ecs_aws.get("service_name"),
                "task_cpu": str(ecs_aws.get("task_cpu", "256")),
                "task_memory": str(ecs_aws.get("task_memory", "512")),
            }
            flat_dep = {
                "strategy": ecs_dep.get("strategy", "rolling"),
                "health_check_path": ecs_dep.get("health_check_path", "/health"),
                "health_check_port": ecs_dep.get("health_check_port", 8080),
                "timeout_minutes": ecs_dep.get("timeout_minutes", 15),
                "min_healthy_percent": ecs_dep.get("min_healthy_percent", 50),
                "max_percent": ecs_dep.get("max_percent", 200),
                "auto_rollback": True,
                "rollback_on_alarm": True,
            }
        elif compute == "ec2":
            ec2_aws = aws_config.get("ec2") or {}
            ec2_dep = dep_config.get("ec2") or {}
            flat_aws = {
                "region": aws_config.get("region", "us-east-1"),
                "ecs_cluster": None,
                "service_name": None,
            }
            flat_dep = {
                "strategy": ec2_dep.get("strategy", "rolling"),
                "health_check_path": ec2_dep.get("health_check_path", "/health"),
                "health_check_port": ec2_dep.get("health_check_port", 8080),
                "timeout_minutes": ec2_dep.get("timeout_minutes", 15),
                "auto_rollback": True,
                "rollback_on_alarm": True,
            }
        else:  # lambda
            lambda_dep = dep_config.get("lambda") or {}
            flat_aws = {
                "region": aws_config.get("region", "us-east-1"),
                "ecs_cluster": None,
                "service_name": None,
            }
            flat_dep = {
                "strategy": "rolling",
                "health_check_path": "/health",
                "health_check_port": 80,
                "timeout_minutes": lambda_dep.get("timeout_minutes", 15),
                "auto_rollback": True,
                "rollback_on_alarm": True,
            }

        # Build the docker_images list from the singular image + dockerfile.
        docker_images: List[Dict[str, Any]] = []
        image = artifacts.get("docker_image")
        if image:
            dockerfile = artifacts.get("dockerfile")
            if not dockerfile:
                # CodeScan deletes its clone after extracting artifacts, so
                # there is never a checked-out source to fall back on , refuse to fabricate an unusable image instead.
                
                raise ValueError(
                    f"InfraCost payload for {image.get('name')!r} carries no "
                    "Dockerfile content; DeployOps cannot build from a "
                    "nonexistent checkout."
                )
            docker_images.append(
                {
                    "name": image.get("name", "app"),
                    "dockerfile": dockerfile,
                    "context": artifacts.get("source_code") or ".",
                    "tag": image.get("tag", "latest"),
                    "platform": "linux/amd64",
                }
            )

        status = approval.get("status", "pending")
        return {
            "job_id": payload.get("job_id"),
            "artifacts": {
                "terraform": artifacts.get("terraform", {"files": {}, "variables": {}}),
                "docker_images": docker_images,
            },
            "aws_config": {
                **flat_aws,
                "assume_role_arn": aws_config.get("assume_role_arn"),
                "target_account_id": aws_config.get("target_account_id"),
            },
            "deployment_config": flat_dep,
            "approval": {
                "deploy_approved": status == "approved",
                "approved_by": approval.get("approved_by"),
            },
        }

    def sanitize_and_validate(self, payload: Dict[str, Any]) -> Dict[str, Any]:         
        """
        Validate and sanitize incoming payload. will return a cleaned payload with defaults for missing optional fields.
        Accepts both the DeployOps-native shape and the InfraCost output format
        (which is normalized to the native shape first).
        """
        payload = self._normalize_payload(payload)

        # 1. Check top-level required fields
        required_top = ["job_id", "artifacts", "aws_config", "deployment_config", "approval"]
        for field in required_top:
            if field not in payload:
                raise ValueError(f"Missing required field: '{field}'")
        
        job_id = payload["job_id"]
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty string")
        # Sanitize job_id: alphanumeric, dash, underscore only
        if not re.match(r'^[a-zA-Z0-9_-]+$', job_id):
            raise ValueError("job_id contains invalid characters")
        
        # 2. Validate artifacts
        artifacts = payload["artifacts"]
        if not isinstance(artifacts, dict):
            raise ValueError("artifacts must be a dictionary")
        
        # 2a. Terraform
        terraform = artifacts.get("terraform")
        if not terraform or not isinstance(terraform, dict):
            raise ValueError("artifacts.terraform is required and must be a dict")
        
        tf_files = terraform.get("files")
        if not tf_files or not isinstance(tf_files, dict):
            raise ValueError("artifacts.terraform.files is required and must be a dict")
        
        # Validate and sanitize each terraform file
        sanitized_tf_files = {}
        for filename, content in tf_files.items():
            # Filename sanitization: only allow alphanumeric, dot, underscore, dash
            if not re.match(r'^[a-zA-Z0-9_.-]+\.tf$', filename):
                raise ValueError(f"Invalid terraform filename: {filename}")
            # Ensure content is string
            if not isinstance(content, str):
                raise ValueError(f"Content of {filename} must be a string")
            # (Optional) More content sanitization? Not needed for terraform.
            sanitized_tf_files[filename] = content
        
        # 2b. Terraform variables (optional)
        tf_vars = terraform.get("variables", {})
        if not isinstance(tf_vars, dict):
            raise ValueError("artifacts.terraform.variables must be a dict")
        # Validate variable keys/values (simple type check)
        for key, value in tf_vars.items():
            if not isinstance(key, str):
                raise ValueError("terraform variable keys must be strings")
            # Values can be string, number, bool, list, dict, or null
            if not isinstance(value, (str, int, float, bool, list, dict, type(None))):
                raise ValueError(f"Invalid type for variable {key}")
        
        # 2c. Docker images (required)
        docker_images = artifacts.get("docker_images")
        if not docker_images or not isinstance(docker_images, list):
            raise ValueError("artifacts.docker_images is required and must be a list")
        if len(docker_images) == 0:
            raise ValueError("artifacts.docker_images must contain at least one image")
        for docker_image in docker_images:
            if not isinstance(docker_image, dict):
                raise ValueError("Each docker_image must be a dict")
            if not docker_image.get("name") or not isinstance(docker_image.get("name"), str):
                raise ValueError("docker_image.name is required and must be a string")
            if not docker_image.get("dockerfile") or not isinstance(docker_image.get("dockerfile"), str):
                raise ValueError("docker_image.dockerfile is required and must be a string")
            if not docker_image.get("context") or not isinstance(docker_image.get("context"), str):
                raise ValueError("docker_image.context is required and must be a string")
            if not docker_image.get("tag") or not isinstance(docker_image.get("tag"), str):
                raise ValueError("docker_image.tag is required and must be a string")
            if not docker_image.get("platform") or not isinstance(docker_image.get("platform"), str):
                raise ValueError("docker_image.platform is required and must be a string")
        
        # 3. AWS Config
        aws_config = payload["aws_config"]
        if not isinstance(aws_config, dict):
            raise ValueError("aws_config must be a dict")
        # Required fields
        required_aws = ["region", "ecs_cluster", "service_name"]
        for field in required_aws:
            if field not in aws_config:
                raise ValueError(f"Missing required aws_config field: {field}")
            if not isinstance(aws_config[field], str) or not aws_config[field].strip():
                raise ValueError(f"aws_config.{field} must be a non-empty string")
        # Sanitize region (simple allowlist? just check format)
        region_pattern = r'^[a-z]{2}-[a-z]+-\d$'
        if not re.match(region_pattern, aws_config["region"]):
            raise ValueError(f"Invalid AWS region: {aws_config['region']}")
        # ECS cluster name: alphanumeric, dash, underscore
        if not re.match(r'^[a-zA-Z0-9_-]+$', aws_config["ecs_cluster"]):
            raise ValueError("Invalid ecs_cluster name")
        if not re.match(r'^[a-zA-Z0-9_-]+$', aws_config["service_name"]):
            raise ValueError("Invalid service_name")
        # Optional CPU/Memory
        task_cpu = aws_config.get("task_cpu", "256")
        task_memory = aws_config.get("task_memory", "512")
        if not isinstance(task_cpu, str) or not task_cpu.isdigit():
            raise ValueError("task_cpu must be a string of digits")
        if not isinstance(task_memory, str) or not task_memory.isdigit():
            raise ValueError("task_memory must be a string of digits")
        
        # 4. Deployment Config
        dep_config = payload["deployment_config"]
        if not isinstance(dep_config, dict):
            raise ValueError("deployment_config must be a dict")
        # Optional fields with default rolling deployment
        strategy = dep_config.get("strategy", "rolling")
        #might remove canary deployment
        if strategy not in ["rolling", "blue-green", "canary"]:
            raise ValueError("deployment_config.strategy must be rolling, blue-green, or canary")
        health_path = dep_config.get("health_check_path", "/health")
        if not isinstance(health_path, str) or not health_path.startswith("/"):
            raise ValueError("health_check_path must start with '/'")
        health_port = dep_config.get("health_check_port", 8080)
        if not isinstance(health_port, int) or health_port <= 0 or health_port > 65535:
            raise ValueError("health_check_port must be a valid port number")
        timeout_min = dep_config.get("timeout_minutes", 5)
        if not isinstance(timeout_min, int) or timeout_min <= 0:
            raise ValueError("timeout_minutes must be a positive integer")
        min_healthy = dep_config.get("min_healthy_percent", 50)
        max_percent = dep_config.get("max_percent", 200)
        if not isinstance(min_healthy, int) or min_healthy < 0 or min_healthy > 100:
            raise ValueError("min_healthy_percent must be between 0 and 100")
        if not isinstance(max_percent, int) or max_percent < 100:
            raise ValueError("max_percent must be at least 100")
        
        # 5. Approval
        approval = payload["approval"]
        if not isinstance(approval, dict):
            raise ValueError("approval must be a dict")
        deploy_approved = approval.get("deploy_approved", False)
        if not isinstance(deploy_approved, bool):
            raise ValueError("approval.deploy_approved must be a boolean")
        approved_by = approval.get("approved_by", "")
        if approved_by and not isinstance(approved_by, str):
            raise ValueError("approved_by must be a string")
        
        # Build sanitized payload to pass to next job
        sanitized = {
            "job_id": job_id,
            "artifacts": {
                "terraform": {
                    "files": sanitized_tf_files,
                    "variables": tf_vars
                },
                "docker_images": [
                    {
                        "name": img["name"],
                        "dockerfile": img["dockerfile"],
                        "context": img["context"],
                        "tag": img["tag"],
                        "platform": img["platform"]
                    }
                    for img in docker_images
                ]
            },
            "aws_config": {
                "region": aws_config["region"],
                "ecs_cluster": aws_config["ecs_cluster"],
                "service_name": aws_config["service_name"],
                "task_cpu": task_cpu,
                "task_memory": task_memory
            },
            "deployment_config": {
                "strategy": strategy,
                "health_check_path": health_path,
                "health_check_port": health_port,
                "timeout_minutes": timeout_min,
                "min_healthy_percent": min_healthy,
                "max_percent": max_percent
            },
            "approval": {
                "deploy_approved": deploy_approved,
                "approved_by": approved_by
            }
        }
        
        self.logger.info(f"Payload validated and sanitized for job {job_id}")
        return sanitized
        
        
    
    