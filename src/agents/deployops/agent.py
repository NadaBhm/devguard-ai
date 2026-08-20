"""
DeployOps Agent
Receives artifacts and deploys to AWS.
"""
import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
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

# agentInfraCost's generated ecs/variables.tf.j2 declares vpc_id, subnet_ids and
# db_* as required vars with no defaults; inject env values into tfvars so
# `terraform plan` never blocks on interactive input.
_ENV_TF_VARS = (
    ("DEVGUARD_VPC_ID", "vpc_id"),
    ("DEVGUARD_SUBNET_IDS", "subnet_ids"),
    ("DEVGUARD_SUBNET_ID", "subnet_id"),
    ("DEVGUARD_DB_HOST", "db_host"),
    ("DEVGUARD_DB_PORT", "db_port"),
    ("DEVGUARD_DB_NAME", "db_name"),
    ("DEVGUARD_DB_USER", "db_user"),
    ("DEVGUARD_DB_PASSWORD", "db_password"),
)


def _terraform_env_vars() -> Dict[str, Any]:
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

    # The EC2 template declares a single required `subnet_id` (singular) while
    # the ECS/S3 templates take `subnet_ids` (plural). DeployOps only knows the
    # plural env var, so derive the singular value from the first subnet when
    # the EC2 var isn't set explicitly.
    if "subnet_id" not in tf_vars and tf_vars.get("subnet_ids"):
        tf_vars["subnet_id"] = tf_vars["subnet_ids"][0]
    return tf_vars


class DeployOpsAgent:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def _check_db_vars_available(tf_dir: Path) -> Optional[str]:
        """Return an error message when terraform requires DB vars the
        deployer didn't supply, else None.

        The ECS template declares db_host/db_port/db_name/db_user/db_password
        as required variables (no default) whenever a database was detected.
        DeployOps fills them from DEVGUARD_DB_* env vars; if any are missing,
        `terraform plan` fails on a cryptic "required variable is not set"
        error after a full image build/push. Detect the requirement from the
        generated variables.tf and fail fast with a clear message.
        """
        variables_tf = tf_dir / "variables.tf"
        if not variables_tf.exists():
            return None
        try:
            content = variables_tf.read_text()
        except OSError:
            return None
        if 'variable "db_host"' not in content:
            return None
        missing = [
            env for env, _ in _ENV_TF_VARS
            if env.startswith("DEVGUARD_DB_") and not os.getenv(env)
        ]
        if missing:
            return (
                "App uses a database that DevGuard does not provision. "
                "Set the database connection before deploying: "
                + ", ".join(missing)
                + ". Deployment cannot run without it."
            )
        return None

    @staticmethod
    def _workspace_dir(job_id: str) -> Path:
        """Central workspace path; was hardcoded in three places that could diverge."""
        root = Path(os.getenv("DEPLOYOPS_WORKSPACE_ROOT", "/tmp/deployops"))
        return root / job_id

    @xray_recorder.capture("deploy")  # type: ignore[reportCallIssue]
    async def deploy(self, payload: Dict[str, Any]) -> Dict[str, Any]:
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

        # DEPLOYOPS_WORKSPACE_ROOT is configurable; /tmp is wiped on restart, which
        # once made every job's workspace and local Terraform state ephemeral.
        workspace_dir = self._workspace_dir(job_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        # Payload carries only Dockerfile + terraform; without a checkout the build
        # context lacks app code and real image builds fail (npm ci). Clone when
        # a repo_url is provided.
        repo_url = (deploy_payload.metadata or {}).get("repo_url")
        if repo_url:
            from src.lib.repo import clone_repo
            try:
                clone_repo(repo_url, workspace_dir)
                self.logger.info(f"Cloned source from {repo_url} into {workspace_dir}")
            except Exception as exc:  # noqa: BLE001 - surface as failed deploy
                self.logger.error(f"Failed to clone source repo {repo_url}: {exc}")
                return {"status": "failed", "job_id": job_id, "error": f"source clone failed: {exc}"}

        await self._write_artifacts(deploy_payload.artifacts, workspace_dir)

        # S3 static sites ship plain files and carry no images, so the loop is a no-op for them.
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

        tf_dir = workspace_dir / "terraform"
        tf_runner = TerraformRunner(tf_dir)

        # A detected database is declared in terraform (variables.tf) as
        # required db_* vars with no defaults, but is NOT provisioned by
        # DevGuard -- DeployOps fills them from DEVGUARD_DB_* env vars. If the
        # deployer didn't supply them, `terraform plan/apply` fails on an
        # obscure "required variable" error after a full image build/push.
        # Fail fast here with a clear message instead.
        db_check = self._check_db_vars_available(tf_dir)
        if db_check is not None:
            return {
                "status": "failed",
                "job_id": job_id,
                "error": db_check,
            }

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

        # Upload before URL resolution so the bucket has objects before any health check.
        if deploy_payload.compute_type == "s3":
            sync_result = await self._sync_static_to_s3(
                workspace_dir, deploy_payload.aws_config, job_id
            )
            if not sync_result["ok"]:
                self.logger.error(f"S3 sync failed: {sync_result['error']}")
                return {
                    "status": "failed",
                    "job_id": job_id,
                    "error": f"s3 sync failed: {sync_result['error']}",
                    "resources": output,
                }

        health_check_path = deploy_payload.deployment_config.health_check_path
        deployed_url = self._resolve_deployed_url(output, deploy_payload)
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

    def _resolve_deployed_url(
        self, output: Dict[str, Any], payload: DeployPayload
    ) -> Optional[str]:
        """Pick the reachable URL from Terraform outputs for any compute type.

        EC2 uses the first of public_ips, S3 its website endpoint; ECS and other
        types fall back to the legacy output keys DeployOps historically read.
        """
        compute = payload.compute_type

        if compute == "ec2":
            public_ips = output.get("public_ips", {}).get("value") or output.get("public_ips")
            if isinstance(public_ips, list) and public_ips:
                port = payload.deployment_config.health_check_port
                return f"http://{public_ips[0]}:{port}"
            public_ip = output.get("public_ip", {}).get("value") or output.get("public_ip")
            if public_ip:
                port = payload.deployment_config.health_check_port
                return f"http://{public_ip}:{port}"

        if compute == "s3":
            website = output.get("website_endpoint", {}).get("value") or output.get("website_endpoint")
            if website:
                return f"http://{website}"

        return (
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

    async def _sync_static_to_s3(
        self, workspace_dir: Path, aws_config: AWSConfig, job_id: str
    ) -> Dict[str, Any]:
        """Upload the site into Terraform's bucket via boto3 (the aws CLI isn't guaranteed). Skips .git and dotfiles."""
        bucket = aws_config.bucket_name
        if not bucket:
            return {"ok": False, "error": "no bucket_name in aws_config"}

        region = aws_config.region
        aws = AWSClient(region=region, assume_role_arn=aws_config.assume_role_arn)
        s3 = aws.session.client("s3", region_name=region, config=RETRY_CONFIG)

        source = workspace_dir
        if not source.exists():
            return {"ok": False, "error": f"source dir {source} missing"}

        # Static sites ship a build output directory (dist/, public/, _site/,
        # build/, out/). Syncing the whole workspace uploads source maps, test
        # files and config that shouldn't be public; prefer the build dir when
        # it actually contains the site's index document, else fall back to the
        # repo root so bare "just HTML in a folder" repos still work.
        source = self._static_source_dir(workspace_dir)

        uploaded = 0
        skipped = 0
        try:
            for filepath in sorted(source.rglob("*")):
                if not filepath.is_file():
                    continue
                rel = filepath.relative_to(source)
                if ".git" in rel.parts or rel.parts[0].startswith("."):
                    skipped += 1
                    continue
                key = rel.as_posix()
                content_type = self._s3_content_type(key)
                s3.upload_file(
                    str(filepath),
                    bucket,
                    key,
                    ExtraArgs={"ContentType": content_type},
                )
                uploaded += 1
            self.logger.info(
                f"S3 sync complete for job {job_id}: {uploaded} uploaded, {skipped} skipped to {bucket}"
            )
            return {"ok": True, "uploaded": uploaded, "skipped": skipped}
        except Exception as exc:  # noqa: BLE001 - surfaced as failed deploy
            self.logger.error(f"S3 sync failed for job {job_id}: {exc}")
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _static_source_dir(workspace_dir: Path) -> Path:
        """Pick the directory whose contents should be uploaded to the S3 site.

        Tries conventional build output dirs (dist/, public/, _site/, build/,
        out/, static/) at the workspace root; the first one that exists and
        actually holds the site's index document wins. Otherwise the whole
        workspace is used so bare "just HTML in a folder" repos still work.
        """
        for candidate in ("dist", "public", "_site", "build", "out", "static"):
            candidate_dir = workspace_dir / candidate
            if not candidate_dir.is_dir():
                continue
            if any(
                (candidate_dir / name).is_file()
                for name in ("index.html", "index.htm")
            ):
                return candidate_dir
        return workspace_dir

    @staticmethod
    def _s3_content_type(key: str) -> str:
        """S3 serves text/plain by default, which breaks HTML/JS/CSS in the browser."""
        ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
        return {
            "html": "text/html; charset=utf-8",
            "htm": "text/html; charset=utf-8",
            "css": "text/css; charset=utf-8",
            "js": "application/javascript; charset=utf-8",
            "mjs": "application/javascript; charset=utf-8",
            "json": "application/json; charset=utf-8",
            "svg": "image/svg+xml",
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "webp": "image/webp",
            "ico": "image/x-icon",
            "woff": "font/woff",
            "woff2": "font/woff2",
            "txt": "text/plain; charset=utf-8",
            "xml": "text/xml; charset=utf-8",
            "pdf": "application/pdf",
        }.get(ext, "application/octet-stream")

    @xray_recorder.capture("_deploy_existing_ecs_revision")  # type: ignore[reportCallIssue]
    async def _deploy_existing_ecs_revision(self, payload: DeployPayload) -> Dict[str, Any]:
        """Create a new ECS task-definition revision for an existing service."""
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
        return {"status": "ready", "agent": "deployops"}

    def app_status(self, app_name: str, environment: str, region: str | None = None) -> Dict[str, Any]:
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

            # ECS drops completed deployments from its response; use registered revisions as durable history.
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

    @xray_recorder.capture("destroy_deployment")  # type: ignore[reportCallIssue]
    async def destroy_deployment(
        self,
        job_id: str,
        aws_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Destroy a job's deployed infrastructure via `terraform destroy`,
        reusing the persisted remote state in TF_STATE_BUCKET (see
        _write_remote_state_backend). Falls back to reporting "no state" for
        deployments made before remote state was enabled, rather than
        pretending to succeed. On failure, describes what's still live in
        AWS (mirrors rollback()/the /monitoring endpoint) so the caller can
        tell the user precisely what to clean up manually -- per the
        feature/destroy-deployment design decisions.
        """
        workspace_dir = self._workspace_dir(job_id)
        tf_dir = workspace_dir / "terraform"
        tf_dir.mkdir(parents=True, exist_ok=True)
        self._write_remote_state_backend(tf_dir, job_id=job_id)

        if not (tf_dir / "backend.tf").exists():
            self.logger.warning(f"[{job_id}] Destroy: TF_STATE_BUCKET not configured, cannot recover state")
            return {
                "status": "no_state",
                "job_id": job_id,
                "message": (
                    "TF_STATE_BUCKET is not configured; this deployment has no "
                    "recoverable Terraform state. Manual AWS cleanup required."
                ),
                "remaining_resources": await self._describe_remaining_resources(job_id, aws_config),
            }

        tf_runner = TerraformRunner(tf_dir)

        if not tf_runner.init():
            self.logger.error(f"[{job_id}] Destroy: terraform init failed")
            return {
                "status": "failed",
                "job_id": job_id,
                "error": "terraform_init_failed",
                "message": "Could not initialize Terraform with the remote state backend.",
                "remaining_resources": await self._describe_remaining_resources(job_id, aws_config),
            }

        # A job with no prior real `apply` (or one applied before
        # TF_STATE_BUCKET existed) has no state key in S3 -- init() still
        # succeeds against an empty backend, so check for an actual state
        # explicitly rather than trusting init() alone.
        state_output = tf_runner.output()
        if not state_output:
            self.logger.info(f"[{job_id}] Destroy: no Terraform state found in remote backend")
            return {
                "status": "no_state",
                "job_id": job_id,
                "message": (
                    "No Terraform state found for this job (deployed before "
                    "remote state was enabled, or state was lost). Nothing to "
                    "destroy via Terraform -- check AWS directly for leftover "
                    "resources."
                ),
                "remaining_resources": await self._describe_remaining_resources(job_id, aws_config),
            }

        try:
            destroyed = tf_runner.destroy()
        except Exception as exc:
            self.logger.error(f"[{job_id}] terraform destroy failed after retries: {exc}")
            return {
                "status": "partial_failure",
                "job_id": job_id,
                "error": str(exc),
                "remaining_resources": await self._describe_remaining_resources(job_id, aws_config),
            }

        if not destroyed:
            return {
                "status": "partial_failure",
                "job_id": job_id,
                "error": "terraform destroy did not report success",
                "remaining_resources": await self._describe_remaining_resources(job_id, aws_config),
            }

        self.logger.info(f"[{job_id}] Destroy successful")
        return {"status": "success", "job_id": job_id}

    async def _describe_remaining_resources(
        self, job_id: str, aws_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Best-effort snapshot of what's still live in AWS after a failed,
        partial, or unrecoverable-state destroy -- so the caller can tell the
        user precisely what to clean up manually and where (mirrors the read
        pattern already used by rollback() and the /monitoring endpoint)."""
        region = aws_config.get("region") or "us-east-1"
        cluster = aws_config.get("ecs_cluster")
        service_name = aws_config.get("service_name")
        remaining: Dict[str, Any] = {
            "ecs_service": None,
            "target_groups": [],
            "error": None,
        }
        if not cluster or not service_name:
            remaining["error"] = "no ecs_cluster/service_name available to check"
            return remaining
        try:
            aws = AWSClient(region=region)
            ecs = aws.ecs()
            desc = ecs.describe_services(cluster=cluster, services=[service_name])
            services = desc.get("services") or []
            if services and services[0].get("status") != "INACTIVE":
                svc = services[0]
                remaining["ecs_service"] = {
                    "cluster": cluster,
                    "service_name": service_name,
                    "status": svc.get("status"),
                    "running_count": svc.get("runningCount"),
                }
                elbv2 = aws.session.client("elbv2", region_name=region, config=RETRY_CONFIG)
                for lb in svc.get("loadBalancers", []):
                    tg_arn = lb.get("targetGroupArn")
                    if tg_arn:
                        remaining["target_groups"].append(tg_arn)
        except Exception as exc:  # noqa: BLE001 - best-effort diagnostic, never raise
            remaining["error"] = str(exc)
        return remaining
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
        """List deployable ECS revisions newest-first, tagging the active one for the rollback-picker UI."""
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

        # Try configured path plus fallbacks; any 200 passes.
        candidates: list[str] = []
        for p in (health_check_path, "/", "/health", "/healthz", "/api/health"):
            if p and p not in candidates:
                candidates.append(p)

        self.logger.info(
            f"Starting health check for {url} candidates={candidates}"
        )
        last_status = 0
        total_start = time.monotonic()

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, max_retries + 1):
                for path in candidates:
                    health_url = f"{url}{path}"
                    attempt_start = time.monotonic()
                    try:
                        response = await client.get(health_url)
                        elapsed_ms = int((time.monotonic() - attempt_start) * 1000)
                        last_status = response.status_code
                        if response.status_code == 200:
                            self.logger.info(
                                f"Health check passed on attempt {attempt} at {path}"
                            )
                            return {
                                "passed": True,
                                "response_time_ms": elapsed_ms,
                                "status_code": 200,
                                "checked_at": datetime.now(timezone.utc).isoformat(),
                                "health_check_path": path,
                            }
                        self.logger.warning(
                            f"Health check attempt {attempt} {path}: status {response.status_code}"
                        )
                    except httpx.TimeoutException:
                        self.logger.warning(f"Health check attempt {attempt} {path}: timeout")
                    except httpx.ConnectError:
                        self.logger.warning(f"Health check attempt {attempt} {path}: connection refused")
                    except Exception as e:
                        self.logger.warning(f"Health check attempt {attempt} {path}: {e}")

                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)

        total_elapsed_ms = int((time.monotonic() - total_start) * 1000)
        self.logger.error(f"Health check failed after {max_retries} attempts")
        return {
            "passed": False,
            "response_time_ms": total_elapsed_ms,
            "status_code": last_status,
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "health_check_path": health_check_path,
        }
        
            
    @staticmethod
    def _write_remote_state_backend(tf_dir: Path, job_id: str) -> None:
        """Write an S3 remote-state backend.tf only when TF_STATE_BUCKET is set.

        Without it, state sits in the job workspace (under /tmp) and is lost on
        restart, with no locking. When unset, do nothing and keep local state.
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
        tf_dir = workspace / "terraform"
        tf_dir.mkdir(parents=True, exist_ok=True)
        self._write_remote_state_backend(tf_dir, job_id=workspace.name)

        tf_root = tf_dir.resolve()
        for filename, content in artifacts.terraform.files.items():
            filepath = tf_root / Path(filename)
            if tf_root not in filepath.resolve().parents:
                raise ValueError(f"Invalid Terraform artifact path: {filename}")
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(content)
            self.logger.info(f"Wrote {filename} to {filepath}")

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
        
        # Merge payload variables with standing-sandbox env-derived values.
        tf_vars = dict(artifacts.terraform.variables or {})
        tf_vars.update(_terraform_env_vars())
        if tf_vars:
            vars_path = tf_dir / "terraform.tfvars.json"
            vars_path.write_text(json.dumps(tf_vars, indent=2))
            self.logger.info(f"Wrote variables to {vars_path}")
        
        # Payloads ship Dockerfile-only contexts; copy repo source when a local context exists.
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


    def _prepare_docker_config(self, docker_config: str) -> str:
        """Provision a DOCKER_CONFIG directory.

        Writes an empty-credsStore config.json (avoids macOS credential-helper
        hangs / `osxkeychain` prompts) and, critically, symlinks the real
        ``~/.docker/cli-plugins`` (buildx et al.) into it. Pointing
        DOCKER_CONFIG at a throwaway directory hides the CLI plugin directory â€”
        ``docker buildx`` then fails with "unknown command: docker buildx"
        (reproduced: ``docker buildx build --load`` runs fine from an
        interactive shell but errors identically from the backend subprocess
        before this fix). Returns the config dir.
        """
        config_path = Path(docker_config) / "config.json"
        if not config_path.exists():
            config_path.write_text('{"credsStore": ""}')
        real_plugins = Path.home() / ".docker" / "cli-plugins"
        local_plugins = Path(docker_config) / "cli-plugins"
        if real_plugins.is_dir() and not local_plugins.exists():
            local_plugins.symlink_to(real_plugins, target_is_directory=True)
        return docker_config

    async def _run_docker_cmd(
        self, cmd: list[str], timeout: float = 300.0, docker_config: str | None = None
    ) -> tuple[int, str, str]:
        """Run a docker command with a DOCKER_CONFIG (avoids macOS credential
        helper hangs). Pass the same ``docker_config`` dir across a
        login/build/push sequence -- each call previously got its own
        throwaway TemporaryDirectory, so `docker login`'s credentials were
        gone (directory deleted) by the time `docker push` ran, failing with
        'no basic auth credentials' on any host without a working global
        Docker credential store (e.g. Windows/Docker Desktop locally).

        Also forces BuildKit (DOCKER_BUILDKIT=1) for every command. The
        legacy builder cannot pull multi-arch images for a
        multi-stage Dockerfile built with ``--platform linux/amd64`` when a
        base image (e.g. node:20-alpine, composer:2) is already cached for
        another platform (e.g. arm64 on an Apple Silicon dev box): the build
        dies with "image with reference sha256:... was found but does not
        provide the specified platform (linux/amd64)". BuildKit handles the
        cross-platform multi-stage pull/build correctly (confirmed live: the
        same Dockerfile the legacy builder rejected built cleanly with
        DOCKER_BUILDKIT=1)."""
        if docker_config is not None:
            self._prepare_docker_config(docker_config)
            env = os.environ.copy()
            env["DOCKER_CONFIG"] = docker_config
            env["DOCKER_BUILDKIT"] = "1"
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return -1, "", f"timed out after {timeout}s"
            return process.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
        with tempfile.TemporaryDirectory() as tmpdir:
            self._prepare_docker_config(tmpdir)
            env = os.environ.copy()
            env["DOCKER_CONFIG"] = tmpdir
            env["DOCKER_BUILDKIT"] = "1"
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return -1, "", f"timed out after {timeout}s"
            return process.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

    @xray_recorder.capture("_build_and_push_image")  # type: ignore[reportCallIssue]
    async def _build_and_push_image(self, docker_image: DockerImageConfig, aws_config: AWSConfig, job_id: str) -> Optional[str]:
        workspace = self._workspace_dir(job_id)

        image_name = docker_image.name
        image_tag = docker_image.tag
        region = aws_config.region

        aws = AWSClient(region=region, assume_role_arn=aws_config.assume_role_arn)
        account_id = aws_config.target_account_id or aws.get_account_id()

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
        decoded_token = base64.b64decode(token).decode("utf-8")
        password = decoded_token.split(":", 1)[1]

        # login/build/push must share ONE DOCKER_CONFIG dir: each was
        # previously calling _run_docker_cmd with its own throwaway
        # TemporaryDirectory, so the credentials `docker login` just wrote
        # were gone (directory already deleted) by the time `docker push`
        # ran -- failed with "no basic auth credentials" on any host without
        # a working global Docker credential store (confirmed on Windows /
        # Docker Desktop locally; masked on the EC2 box by a pre-existing
        # manual `docker login` in the real ~/.docker/config.json).
        with tempfile.TemporaryDirectory() as docker_config_dir:
            returncode, stdout, stderr = await self._run_docker_cmd(
                ["docker", "login", "--username", "AWS", "--password", password, proxy_endpoint],
                timeout=60.0, docker_config=docker_config_dir,
            )
            if returncode != 0:
                self.logger.error(f"ECR login failed: {stderr}")
                return None

            # ECR repository URI
            ecr_repo = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{image_name}"
            image_uri = f"{ecr_repo}:{image_tag}"

            # Build Docker image with platform for Fargate compatibility.
            # Use `docker buildx build` (BuildKit) directly: plain `docker build`
            # on a host whose default builder is BuildKit-first, and the legacy
            # builder both misbehave with cross-platform multi-stage builds
            # (legacy chokes reusing an image cached for another platform, and
            # DOCKER_BUILDKIT=1 via `docker build` errors "BuildKit is enabled
            # but the buildx component is missing or broken"). `--load` lands the
            # image in the local image store so `docker push` can find it.
            build_context = self._workspace_dir(job_id) / docker_image.context
            returncode, stdout, stderr = await self._run_docker_cmd(
                ["docker", "buildx", "build", "--load", "--platform", docker_image.platform, "-t", image_uri, str(build_context)],
                timeout=600.0, docker_config=docker_config_dir,
            )
            if returncode != 0:
                self.logger.error(f"Docker build failed: {stderr}")
                return None

            # Push to ECR. A php-ext image with a compiled mysqli/pdo layer can
            # exceed 400MB compressed across a modest uplink; the 300s default
            # (which used to be the push timeout) killed a 240MB image at ~534s
            # (measured live), so push gets a generous 900s budget.
            returncode, stdout, stderr = await self._run_docker_cmd(
                ["docker", "push", image_uri], timeout=900.0, docker_config=docker_config_dir,
            )
            if returncode != 0:
                self.logger.error(f"Docker push failed: {stderr}")
                return None

        self.logger.info(f"Image pushed: {image_uri}")
        return image_uri
    
    async def _build_and_push_backend_image(self, backend_image_uri: str, region: str, account_id: str, job_id: str) -> bool:
        # URI format: account.dkr.ecr.region.amazonaws.com/repo:tag
        repo_name = backend_image_uri.split('/')[-1].split(':')[0]
        ecr_repo = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{repo_name}"
        
        backend_dockerfile = Path("testing/three-tier-app/backend/Dockerfile")
        if not backend_dockerfile.exists():
            self.logger.warning(f"Backend Dockerfile not found at {backend_dockerfile}")
            return False
        
        build_cmd = ["docker", "buildx", "build", "--load", "--platform", "linux/amd64", "-t", backend_image_uri, str(backend_dockerfile.parent)]
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
        if "compute_type" not in payload:
            return payload

        compute = payload.get("compute_type", "ecs")
        artifacts = payload.get("artifacts", {})
        aws_config = payload.get("aws_config", {})
        dep_config = payload.get("deployment_config", {})
        approval = payload.get("approval", {})

        if compute == "ecs":
            ecs_aws = aws_config.get("ecs") or {}
            ecs_dep = dep_config.get("ecs") or {}
            # The orchestrator adapter already flattens aws_config (top-level
            # ecs_cluster / service_name) while still tagging compute_type;
            # prefer those flat values so re-normalization doesn't null them
            # out. Nested (InfraCost-raw) shapes keep working as before.
            flat_aws = {
                "region": aws_config.get("region", "us-east-1"),
                "ecs_cluster": aws_config.get("ecs_cluster") or ecs_aws.get("cluster"),
                "service_name": aws_config.get("service_name") or ecs_aws.get("service_name"),
                "task_cpu": str(
                    aws_config.get("task_cpu") or ecs_aws.get("task_cpu", "256")
                ),
                "task_memory": str(
                    aws_config.get("task_memory") or ecs_aws.get("task_memory", "512")
                ),
            }
            flat_dep = {
                "strategy": ecs_dep.get("strategy") or dep_config.get("strategy", "rolling"),
                "health_check_path": ecs_dep.get("health_check_path")
                    or dep_config.get("health_check_path", "/health"),
                "health_check_port": ecs_dep.get("health_check_port")
                    or dep_config.get("health_check_port", 8080),
                "timeout_minutes": ecs_dep.get("timeout_minutes")
                    or dep_config.get("timeout_minutes", 15),
                "min_healthy_percent": ecs_dep.get("min_healthy_percent")
                    or dep_config.get("min_healthy_percent", 50),
                "max_percent": ecs_dep.get("max_percent")
                    or dep_config.get("max_percent", 200),
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
                "strategy": ec2_dep.get("strategy") or dep_config.get("strategy", "rolling"),
                "health_check_path": ec2_dep.get("health_check_path")
                    or dep_config.get("health_check_path", "/health"),
                "health_check_port": ec2_dep.get("health_check_port")
                    or dep_config.get("health_check_port", 8080),
                "timeout_minutes": ec2_dep.get("timeout_minutes")
                    or dep_config.get("timeout_minutes", 15),
                "auto_rollback": True,
                "rollback_on_alarm": True,
            }
        elif compute == "s3":
            s3_aws = aws_config.get("s3") or {}
            s3_dep = dep_config.get("s3") or {}
            flat_aws = {
                "region": aws_config.get("region", "us-east-1"),
                "ecs_cluster": None,
                "service_name": None,
                "bucket_name": aws_config.get("bucket_name") or s3_aws.get("bucket_name"),
            }
            flat_dep = {
                "strategy": s3_dep.get("strategy", "static"),
                "health_check_path": s3_dep.get("health_check_path", "/"),
                "health_check_port": 80,
                "timeout_minutes": s3_dep.get("timeout_minutes", 15),
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

        docker_images: List[Dict[str, Any]] = []
        # Preserve an already-correct plural docker_images list (DeployOps-native
        # payloads) instead of rebuilding from the older singular docker_image
        # shape, which silently emptied it and broke every real deployment.
        existing = artifacts.get("docker_images")
        if isinstance(existing, list) and existing:
            docker_images = existing
        image = artifacts.get("docker_image")
        if image and not docker_images:
            dockerfile = artifacts.get("dockerfile")
            if not dockerfile:
                # CodeScan deletes its clone after extracting artifacts; never fabricate an image.
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
        deploy_approved = approval.get("deploy_approved")
        if deploy_approved is None:
            deploy_approved = status == "approved"
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
                "deploy_approved": deploy_approved,
                "approved_by": approval.get("approved_by"),
            },
            "metadata": payload.get("metadata", {}) or {},
            "compute_type": compute,
        }

    def sanitize_and_validate(self, payload: Dict[str, Any]) -> Dict[str, Any]:         
        """Validate/sanitize a payload; accepts DeployOps-native or InfraCost shapes (normalized first)."""
        payload = self._normalize_payload(payload)

        required_top = ["job_id", "artifacts", "aws_config", "deployment_config", "approval"]
        for field in required_top:
            if field not in payload:
                raise ValueError(f"Missing required field: '{field}'")
        
        job_id = payload["job_id"]
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty string")
        if not re.match(r'^[a-zA-Z0-9_-]+$', job_id):
            raise ValueError("job_id contains invalid characters")
        
        artifacts = payload["artifacts"]
        if not isinstance(artifacts, dict):
            raise ValueError("artifacts must be a dictionary")
        
        terraform = artifacts.get("terraform")
        if not terraform or not isinstance(terraform, dict):
            raise ValueError("artifacts.terraform is required and must be a dict")
        
        tf_files = terraform.get("files")
        if not tf_files or not isinstance(tf_files, dict):
            raise ValueError("artifacts.terraform.files is required and must be a dict")
        
        sanitized_tf_files = {}
        for filename, content in tf_files.items():
            if not re.match(r'^[a-zA-Z0-9_.-]+\.tf$', filename):
                raise ValueError(f"Invalid terraform filename: {filename}")
            if not isinstance(content, str):
                raise ValueError(f"Content of {filename} must be a string")
            sanitized_tf_files[filename] = content
        
        tf_vars = terraform.get("variables", {})
        if not isinstance(tf_vars, dict):
            raise ValueError("artifacts.terraform.variables must be a dict")
        for key, value in tf_vars.items():
            if not isinstance(key, str):
                raise ValueError("terraform variable keys must be strings")
            if not isinstance(value, (str, int, float, bool, list, dict, type(None))):
                raise ValueError(f"Invalid type for variable {key}")
        
        # s3 static sites ship plain files and carry no container images.
        compute_type = payload.get("compute_type", "ecs")
        docker_images = artifacts.get("docker_images")
        if not isinstance(docker_images, list):
            raise ValueError("artifacts.docker_images is required and must be a list")
        if compute_type != "s3" and len(docker_images) == 0:
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
            if not re.match(r'^[a-zA-Z0-9_][a-zA-Z0-9._-]{0,127}$', docker_image["tag"]):
                raise ValueError("docker_image.tag contains invalid characters")
            context_path = Path(docker_image["context"])
            if context_path.is_absolute() or ".." in context_path.parts:
                raise ValueError(
                    "docker_image.context must be a safe relative path inside the workspace"
                )
        
        aws_config = payload["aws_config"]
        if not isinstance(aws_config, dict):
            raise ValueError("aws_config must be a dict")
        if "region" not in aws_config:
            raise ValueError("Missing required aws_config field: region")
        if not isinstance(aws_config["region"], str) or not aws_config["region"].strip():
            raise ValueError("aws_config.region must be a non-empty string")
        region_pattern = r'^[a-z]{2}-[a-z]+-\d$'
        if not re.match(region_pattern, aws_config["region"]):
            raise ValueError(f"Invalid AWS region: {aws_config['region']}")
        # ecs_cluster / service_name are only meaningful for ECS deployments;
        # EC2 (instance) and S3 (static site) payloads carry null by design.
        if compute_type == "ecs":
            for field in ["ecs_cluster", "service_name"]:
                if field not in aws_config:
                    raise ValueError(f"Missing required aws_config field: {field}")
                if not isinstance(aws_config[field], str) or not aws_config[field].strip():
                    raise ValueError(f"aws_config.{field} must be a non-empty string")
            if not re.match(r'^[a-zA-Z0-9_-]+$', aws_config["ecs_cluster"]):
                raise ValueError("Invalid ecs_cluster name")
            if not re.match(r'^[a-zA-Z0-9_-]+$', aws_config["service_name"]):
                raise ValueError("Invalid service_name")
        task_cpu = aws_config.get("task_cpu", "256")
        task_memory = aws_config.get("task_memory", "512")
        if not isinstance(task_cpu, str) or not task_cpu.isdigit():
            raise ValueError("task_cpu must be a string of digits")
        if not isinstance(task_memory, str) or not task_memory.isdigit():
            raise ValueError("task_memory must be a string of digits")
        
        dep_config = payload["deployment_config"]
        if not isinstance(dep_config, dict):
            raise ValueError("deployment_config must be a dict")
        strategy = dep_config.get("strategy", "rolling")
        if strategy not in ["rolling", "blue-green", "canary", "static"]:
            raise ValueError("deployment_config.strategy must be rolling, blue-green, canary, or static")
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
        
        approval = payload["approval"]
        if not isinstance(approval, dict):
            raise ValueError("approval must be a dict")
        deploy_approved = approval.get("deploy_approved", False)
        if not isinstance(deploy_approved, bool):
            raise ValueError("approval.deploy_approved must be a boolean")
        approved_by = approval.get("approved_by", "")
        if approved_by and not isinstance(approved_by, str):
            raise ValueError("approved_by must be a string")
        
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
                "ecs_cluster": aws_config.get("ecs_cluster") if compute_type == "ecs" else None,
                "service_name": aws_config.get("service_name") if compute_type == "ecs" else None,
                "task_cpu": task_cpu,
                "task_memory": task_memory,
                "bucket_name": aws_config.get("bucket_name") if compute_type == "s3" else None,
                "assume_role_arn": aws_config.get("assume_role_arn"),
                "target_account_id": aws_config.get("target_account_id"),
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
            },
            "metadata": payload.get("metadata", {}) or {},
            "compute_type": compute_type
        }
        
        self.logger.info(f"Payload validated and sanitized for job {job_id}")
        return sanitized
        
        
    
    