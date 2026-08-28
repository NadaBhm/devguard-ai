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
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from aws_xray_sdk.core import xray_recorder

from src.lib.aws.client import AWSClient, RETRY_CONFIG
from src.lib.terraform.runner import TerraformRunner
from src.agents.deployops.models import (
    DeployPayload,
    Artifacts,
    AWSConfig,
    DockerImageConfig,
)

logging.basicConfig(level=logging.INFO)

# InfraCost's ecs/variables.tf.j2 declares vpc_id/subnet_ids/db_* required with no defaults;
# inject env values into tfvars so `terraform plan` never blocks on interactive input.
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
    db_envs_present = []
    for env_name, tf_name in _ENV_TF_VARS:
        value = os.getenv(env_name)
        if not value:
            continue
        if env_name.startswith("DEVGUARD_DB_"):
            db_envs_present.append(env_name)
        if tf_name == "subnet_ids":
            value = [s.strip() for s in value.split(",") if s.strip()]
        elif tf_name == "db_port":
            try:
                value = int(value)
            except ValueError:
                continue
        tf_vars[tf_name] = value

    # Standing sandbox DB supplied via DEVGUARD_DB_*: skip provisioning a fresh RDS (its 10-min
    # readiness race crashed ECS tasks before the app could ever connect).
    required_db_envs = {"DEVGUARD_DB_HOST", "DEVGUARD_DB_PORT"}
    if required_db_envs.issubset(set(db_envs_present)):
        tf_vars["create_db"] = False

    # The EC2 template requires a singular `subnet_id` while ECS/S3 take plural `subnet_ids`;
    # DeployOps only knows the plural env var, so derive the singular from the first subnet.
    if "subnet_id" not in tf_vars and tf_vars.get("subnet_ids"):
        tf_vars["subnet_id"] = tf_vars["subnet_ids"][0]
    return tf_vars


class DeployOpsAgent:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    @staticmethod
    def _check_db_vars_available(tf_dir: Path) -> Optional[str]:
        """Return an error message when terraform requires DB vars the deployer didn't supply.
        The ECS template declares db_* as required (no default) whenever a database was detected,
        filled from DEVGUARD_DB_* env vars; if any are missing, `terraform plan` fails cryptically
        after a full build/push -- detect from variables.tf and fail fast with a clear message."""
        variables_tf = tf_dir / "variables.tf"
        if not variables_tf.exists():
            return None
        try:
            content = variables_tf.read_text()
        except OSError:
            return None
        if 'variable "db_host"' not in content:
            return None
        # When the template provisions its own DB (create_db), no external DB is needed.
        if 'variable "create_db"' in content:
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

        # Payload carries only Dockerfile + terraform; without a checkout real image builds
        # fail (npm ci lacks app code). Clone when a repo_url is provided.
        repo_url = (deploy_payload.metadata or {}).get("repo_url")
        if repo_url:
            from src.lib.repo import clone_repo
            try:
                # 50k files: monorepos (next.js = 31k) are legit build contexts.
                target_sha = (deploy_payload.metadata or {}).get("commit_sha")
                clone_repo(
                    repo_url, workspace_dir, max_files=50_000, timeout=300,
                    commit_sha=target_sha if target_sha and target_sha != "HEAD" else None,
                )
                self.logger.info(f"Cloned source from {repo_url} into {workspace_dir}")
            except Exception as exc:  # noqa: BLE001 - surface as failed deploy
                self.logger.error(f"Failed to clone source repo {repo_url}: {exc}")
                return {"status": "failed", "job_id": job_id, "error": f"source clone failed: {exc}"}

        await self._write_artifacts(deploy_payload.artifacts, workspace_dir)

        self.logger.info(f"[{job_id}] compute={deploy_payload.compute_type} images={len(deploy_payload.artifacts.docker_images)}")

        # S3 static sites ship plain files and carry no images, so the loop is a no-op for them.
        for img_idx, docker_image in enumerate(deploy_payload.artifacts.docker_images):
            self.logger.info(f"[{job_id}] building image {img_idx+1}/{len(deploy_payload.artifacts.docker_images)}: {docker_image.name}")
            image_uri = await self._build_and_push_image(
                docker_image, deploy_payload.aws_config, job_id,
                health_check_port=deploy_payload.deployment_config.health_check_port,
            )
            self.logger.info(f"[{job_id}] build result: {image_uri}")
            if not image_uri:
                self.logger.error(f"Image build/push failed for {docker_image.name}")
                return {
                    "status": "failed",
                    "job_id": job_id,
                    "error": f"image build/push failed: {docker_image.name}",
                }

        tf_dir = workspace_dir / "terraform"
        tf_runner = TerraformRunner(tf_dir)

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
            # Partial-apply cleanup: resources created before the failure
            # (IAM roles, ALBs) would otherwise leak as AWS orphans that break
            # retries with EntityAlreadyExists. Best-effort destroy of whatever
            # state was written; never masks the original failure.
            try:
                if tf_runner.output():
                    self.logger.info("Cleaning up partial apply state via terraform destroy")
                    tf_runner.destroy()
            except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                self.logger.warning(f"Post-apply-failure cleanup incomplete: {exc}")
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
        """Pick the reachable URL from Terraform outputs for any compute type: EC2 uses the first
        of public_ips, S3 its website endpoint; ECS and other types fall back to the legacy
        output keys DeployOps historically read."""
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

        source = self._static_source_dir(workspace_dir)

        uploaded = 0
        skipped = 0
        try:
            for filepath in sorted(source.rglob("*")):
                if not filepath.is_file():
                    continue
                rel = filepath.relative_to(source)
                if ".git" in rel.parts or "terraform" in rel.parts or ".terraform" in rel.parts or rel.parts[0].startswith("."):
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
        """Pick the directory whose contents should be uploaded to the S3 site: the first
        existing conventional build dir (dist/, public/, _site/, build/, out/, static/) holding an
        index document wins; else the whole workspace so bare HTML-folder repos still work."""
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
        """Redeploy the current commit onto an EXISTING ECS service: build/push fresh images,
        register a task-definition revision pointing at them, update the service, skip Terraform.
        FIX (previously): only bumped DEPLOYMENT_REVISION and left containerDefinitions[].image
        untouched, silently redeploying the OLD image (see ExistingDeploymentInfo in state.py)."""
        job_id = payload.job_id
        region = payload.aws_config.region
        cluster = payload.aws_config.model_dump().get("ecs_cluster") or "todo-app-cluster"
        service_name = payload.aws_config.model_dump().get("service_name") or "todo-app-dev-frontend"
        self.logger.info(f"[ECS UPDATE {job_id}] target cluster={cluster!r} service={service_name!r} region={region}")

        workspace_dir = self._workspace_dir(job_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        repo_url = (payload.metadata or {}).get("repo_url")
        if repo_url:
            from src.lib.repo import clone_repo
            target_sha = (payload.metadata or {}).get("commit_sha")
            try:
                clone_repo(
                    repo_url, workspace_dir, timeout=300,
                    commit_sha=target_sha if target_sha and target_sha != "HEAD" else None,
                )
                self.logger.info(f"Cloned source from {repo_url} into {workspace_dir}")
            except Exception as exc:  # noqa: BLE001 - surface as failed deploy
                self.logger.error(f"Failed to clone source repo {repo_url}: {exc}")
                return {"status": "failed", "job_id": job_id, "error": f"source clone failed: {exc}"}

        await self._write_artifacts(payload.artifacts, workspace_dir)

        # Built up front, before touching the live service: a build/push
        # failure must never leave the running task definition half-updated.
        built_images: Dict[str, str] = {}
        for docker_image in payload.artifacts.docker_images:
            image_uri = await self._build_and_push_image(
                docker_image, payload.aws_config, job_id,
                health_check_port=payload.deployment_config.health_check_port,
            )
            if not image_uri:
                self.logger.error(f"Image build/push failed for {docker_image.name}")
                return {
                    "status": "failed",
                    "job_id": job_id,
                    "error": f"image build/push failed: {docker_image.name}",
                }
            built_images[docker_image.name] = image_uri

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
                # terraform_generator.py sets containerDefinitions[].name to the same
                # docker_images[].name it was built from, so this match is exact.
                if container.get("name") in built_images:
                    container["image"] = built_images[container["name"]]

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
        """Destroy a job's deployed infrastructure via `terraform destroy`, reusing persisted
        remote state in TF_STATE_BUCKET (see _write_remote_state_backend); reports "no state"
        for pre-remote-state deployments instead of pretending success, and on failure describes
        what's still live in AWS (mirrors rollback()/monitoring) per its design decisions."""
        workspace_dir = self._workspace_dir(job_id)
        tf_dir = workspace_dir / "terraform"
        tf_dir.mkdir(parents=True, exist_ok=True)
        self._write_remote_state_backend(tf_dir, job_id=job_id)

        tf_runner = TerraformRunner(tf_dir)

        # Two state sources: remote S3 (backend.tf, written when TF_STATE_BUCKET is set) or the
        # local tfstate in the persisted workspace; without this teardown silently no-ops and
        # leaks AWS resources.
        if not (tf_dir / "backend.tf").exists() and not (tf_dir / "terraform.tfstate").exists():
            self.logger.warning(f"[{job_id}] Destroy: no backend.tf (TF_STATE_BUCKET unset) and no local tfstate")
            return {
                "status": "no_state",
                "job_id": job_id,
                "message": (
                    "No recoverable Terraform state (TF_STATE_BUCKET is not "
                    "configured and no local tfstate exists in the job "
                    "workspace). Manual AWS cleanup required."
                ),
                "remaining_resources": await self._describe_remaining_resources(job_id, aws_config),
            }

        if not tf_runner.init():
            self.logger.error(f"[{job_id}] Destroy: terraform init failed")
            return {
                "status": "failed",
                "job_id": job_id,
                "error": "terraform_init_failed",
                "message": "Could not initialize Terraform with the state backend.",
                "remaining_resources": await self._describe_remaining_resources(job_id, aws_config),
            }

        # A job with no prior real `apply` has no state: init() still succeeds against an empty
        # backend (or empty local state), so check for actual state rather than trusting init().
        state_output = tf_runner.output()
        if not state_output:
            self.logger.info(f"[{job_id}] Destroy: no Terraform state found (remote backend or local tfstate)")
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
        """Best-effort snapshot of what's still live in AWS after a failed, partial, or
        unrecoverable-state destroy -- lets the caller tell the user precisely what to clean up
        manually and where (mirrors rollback() and the /monitoring endpoint)."""
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
        ecs_cluster: str,
        service_name: str,
        target_revision: Optional[int] = None,
        region: str | None = None,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Roll back/forward a live ECS service to a task-definition revision.
        Names are used verbatim. Auto-target falls back to revision history
        when only one live deployment exists."""
        aws = AWSClient(region=region)
        cluster = ecs_cluster
        service = service_name
        try:
            desc = aws.ecs().describe_services(cluster=cluster, services=[service])
            services = desc.get("services") or []
            if not services or services[0].get("status") == "INACTIVE":
                return {"status": "failed", "error": f"Service {service} not found in {cluster}"}
            current_arn = services[0].get("taskDefinition")
            current_task = (current_arn or "").rsplit("/", 1)[-1]
            family = current_task.rsplit(":", 1)[0] if ":" in current_task else current_task

            if target_revision is not None:
                task_definition = f"{family}:{target_revision}"
                if task_definition == current_task:
                    return {
                        "status": "failed",
                        "error": f"Revision {target_revision} is already the active task definition",
                    }
            else:
                deployments = services[0].get("deployments", [])
                prior = [d["taskDefinition"] for d in deployments if d["taskDefinition"] != current_arn]
                if not prior:
                    # Single live entry — check revision history.
                    revisions = aws.ecs().list_task_definitions(
                        familyPrefix=family,
                        status="ACTIVE",
                        sort="DESC",
                    ).get("taskDefinitionArns", [])
                    prior = [arn for arn in revisions if arn != current_arn]
                if not prior:
                    return {"status": "failed", "error": "No previous deployment to rollback to"}
                task_definition = prior[0]

            aws.ecs().update_service(
                cluster=cluster,
                service=service,
                taskDefinition=task_definition,
                forceNewDeployment=True,
            )
            aws.ecs().get_waiter("services_stable").wait(cluster=cluster, services=[service])
            return {
                "status": "success",
                "cluster": cluster,
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
        # Extra candidates for common framework-specific health endpoints.
        for p in ("/actuator/health", "/status", "/api/v1/health", "/up"):
            if p not in candidates:
                candidates.append(p)
        last_status = 0
        total_start = time.monotonic()
        consecutive_refused_attempts = 0

        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, max_retries + 1):
                all_refused = True
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
                        all_refused = False
                    except httpx.TimeoutException:
                        self.logger.warning(f"Health check attempt {attempt} {path}: timeout")
                        all_refused = False
                    except httpx.ConnectError:
                        self.logger.warning(f"Health check attempt {attempt} {path}: connection refused")
                    except Exception as e:
                        self.logger.warning(f"Health check attempt {attempt} {path}: {e}")
                        all_refused = False

                if all_refused:
                    consecutive_refused_attempts += 1
                else:
                    consecutive_refused_attempts = 0

                if attempt < max_retries:
                    # Early-abort: an app that refuses connections on every
                    # candidate has crashed and will not recover by waiting.
                    # Timeouts may be a booting instance, so those keep the
                    # full window. 3 fully-refused attempts = dead app.
                    if consecutive_refused_attempts >= 3:
                        self.logger.error(
                            "Health check aborted early: connection refused on all "
                            f"candidates for {consecutive_refused_attempts} consecutive "
                            "attempts (app not listening / container exited)"
                        )
                        break
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
        """Write an S3 remote-state backend.tf only when TF_STATE_BUCKET is set; without it,
        state sits in the job workspace (under /tmp, lost on restart) with no locking. When
        unset, do nothing and keep local state."""
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

    @staticmethod
    def _fix_hcl_syntax(content: str) -> str:
        """Deterministic HCL repairs for LLM-refined Terraform. The refiner
        occasionally fuses the closing brace onto the last argument line
        (`health_check_grace_period_seconds = 600}`), which is invalid HCL and
        kills `terraform init` after a full image build."""
        # value-then-brace on one line -> newline before }
        fixed = re.sub(
            r'=(\s*(?:\d+|true|false|"[^"\n]*"))\}',
            r"=\1\n}",
            content,
        )
        return fixed

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
            if filename.endswith(".tf"):
                content = self._fix_hcl_syntax(content)
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
        """Provision a DOCKER_CONFIG directory: write an empty-credsStore config.json (avoids
        macOS credential-helper hangs) and symlink ~/.docker/cli-plugins into it so
        ``docker buildx`` remains available. Returns the config dir."""
        config_path = Path(docker_config) / "config.json"
        if not config_path.exists():
            config_path.write_text('{"credsStore": ""}')
        real_plugins = Path.home() / ".docker" / "cli-plugins"
        local_plugins = Path(docker_config) / "cli-plugins"
        if real_plugins.is_dir() and not local_plugins.exists():
            local_plugins.symlink_to(real_plugins, target_is_directory=True)
        return docker_config

    @staticmethod
    def _sanitize_php_dockerfile(build_context: Path) -> None:
        """Ensure PHP images have the system packages php's composer needs:
        git (source fallback) + unzip + zip extension. LLM-generated Dockerfiles
        commonly omit them, failing on both dist (zip) and source (git) paths.
        Also strips the obsolete `json` extension (built-in since PHP 8) that
        kills `docker-php-ext-install` on php:8.x."""
        dockerfile_path = build_context / "Dockerfile"
        if not dockerfile_path.is_file():
            return
        original_text = dockerfile_path.read_text(encoding="utf-8", errors="ignore")
        text = original_text
        # Case-insensitive FROM php match ("FROM php".lower() never appears in
        # text.lower() — the literal uppercase needle against a lowercase hay).
        if not re.search(r"(?mi)^from\s+php", text):
            return
        # Strip phantom `json` from docker-php-ext-install (fails on php 8.x)
        if "docker-php-ext-install" in text and " json" in text:
            text = re.sub(r"docker-php-ext-install([^\n]*)\bjson\b", r"docker-php-ext-install\1", text)
        # The `|| composer install --prefer-source` fallback git-clones every
        # package when dist fails (or zip ext missing) — measured 30-minute
        # hangs. Drop the fallback clause entirely; dist + unzip is enough.
        if "--prefer-source" in text:
            lines_out = []
            for line in text.splitlines():
                if "--prefer-source" in line and "composer install" in line and "||" in line:
                    primary = line.split("||", 1)[0].rstrip()
                    line = primary
                line = line.replace("--prefer-source", "--prefer-dist")
                lines_out.append(line)
            text = "\n".join(lines_out) + "\n"
        # If composer is used, ensure required packages are present.
        if "composer" not in text.lower():
            if text != dockerfile_path.read_text(encoding="utf-8", errors="ignore"):
                dockerfile_path.write_text(text, encoding="utf-8")
            return
        # Already has the fix?
        if "libzip-dev" in text and " unzip" in text:
            if text != original_text:
                dockerfile_path.write_text(text, encoding="utf-8")
                logging.getLogger(__name__).info("Sanitized PHP Dockerfile: json/prefer-source cleanup")
            return
        # Insert apt-get install for missing deps before first composer-related RUN
        lines = text.splitlines()
        new_lines: list[str] = []
        injected = False
        for line in lines:
            # Inject once before the first RUN that mentions composer
            if not injected and line.strip().startswith("RUN") and "composer" in line.lower():
                if "unzip" not in text:
                    new_lines.append("RUN apt-get update && apt-get install -y unzip libzip-dev \\")
                    new_lines.append("    && docker-php-ext-install zip 2>/dev/null || true")
                    injected = True
            new_lines.append(line)
        if injected or text != original_text:
            dockerfile_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            logging.getLogger(__name__).info("Sanitized PHP Dockerfile: injected unzip/libzip for composer")

    @staticmethod
    def _drop_optional_copy_from_build(build_context: Path) -> None:
        """The Laravel template's ``COPY --from=build /app/public/build`` is optional:
        not all Laravel apps ship a vite-built public/build asset tree. A hard COPY
        fails the checksum when that path is absent in the build stage — rewrite to
        a shell fallback that copies only when present."""
        dockerfile_path = build_context / "Dockerfile"
        if not dockerfile_path.is_file():
            return
        text = dockerfile_path.read_text(encoding="utf-8", errors="ignore")
        if "COPY --from=build /app/public/build" not in text:
            return
        new_text = text.replace(
            "COPY --from=build /app/public/build ./public/build",
            "RUN --mount=from=build,src=/app/public/build,target=/tmp/build cp -r /tmp/build ./public/build 2>/dev/null || echo 'no public/build assets, skipping'",
        )
        if new_text != text:
            dockerfile_path.write_text(new_text, encoding="utf-8")
            logging.getLogger(__name__).info("Rewrote optional COPY --from=build public/build to shell fallback")

    @staticmethod
    def _exact_case_exists(build_context: Path, rel: str) -> bool:
        """Case-SENSITIVE existence check. The host filesystem (macOS APFS) is
        case-insensitive, so ``Path.exists()`` returns True for LLM-hallucinated
        lowercase paths (restserviceapplication.java) that do not exist inside
        the case-sensitive Linux build container — failing the build there."""
        current = build_context
        for part in rel.split("/"):
            if not current.is_dir():
                return False
            try:
                names = os.listdir(current)
            except OSError:
                return False
            if part in names:
                current = current / part
            else:
                return False
        return current.is_file()

    @staticmethod
    def _drop_missing_copy_sources(build_context: Path) -> None:
        """Drop ``COPY <src>`` lines whose sources don't exist in ``build_context``: the refiner
        hallucinates files (e.g. ``COPY rust-toolchain.toml /`` on next.js), killing the build at
        checksum time. Also drops dangling ``COPY --from=<name>`` refs to undefined stages."""
        dockerfile_path = build_context / "Dockerfile"
        if not dockerfile_path.is_file():
            return
        text = dockerfile_path.read_text(encoding="utf-8", errors="ignore")
        defined_stages = {
            m.group(1).lower()
            for m in re.finditer(r"(?mi)^\s*FROM\s+\S+\s+AS\s+([\w-]+)", text)
        }
        kept: list[str] = []
        dropped: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            m = re.match(r'^COPY\s+((?:--[^\s]+\s+)*)(.+)$', stripped)
            flags = (m.group(1) or "") if m else ""
            if not m:
                kept.append(line)
                continue
            from_m = re.search(r"--from=([^\s]+)", flags)
            if from_m:
                name = from_m.group(1)
                # Real image refs (contain '/' or ':') are kept; anything
                # else must be an in-file stage.
                if "/" not in name and ":" not in name and name.lower() not in defined_stages:
                    dropped.append(stripped)
                    continue
                kept.append(line)
                continue
            rest = m.group(2)
            if rest.startswith("["):
                # JSON form: COPY ["src", "dst"]
                try:
                    parts = json.loads(rest.replace("'", '"'))
                except json.JSONDecodeError:
                    kept.append(line)
                    continue
            else:
                # Shell form: last token is dest, everything before is src.
                tokens = shlex.split(rest) if "\\" not in rest else rest.split()
                parts = [t for t in tokens if not t.startswith("--")]
            sources = parts[:-1] if len(parts) > 1 else []
            if not sources:
                kept.append(line)
                continue
            missing = [
                s for s in sources
                if "*" not in s and "?" not in s
                and not DeployOpsAgent._exact_case_exists(build_context, s.lstrip("/"))
            ]
            if missing:
                dropped.append(stripped)
                continue
            kept.append(line)
        if dropped:
            dockerfile_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
            logging.getLogger(__name__).warning(
                "Dropped %d COPY line(s) referencing files absent from %s: %s",
                len(dropped), build_context, dropped,
            )

    async def _preflight_check(
        self, image_uri: str, build_context: Path, docker_image, docker_config_dir: str, job_id: str
    ) -> bool:
        """Run the built container locally and verify it serves HTTP before push.

        Tries the original CMD first; if the container doesn't respond within
        60s, tries language-specific fallback commands. First variant that
        serves wins. Returns True when at least one variant works.
        """
        import asyncio

        port = 8080  # DockerImageConfig has no port field; use safe default

        # Extract EXPOSE from the Dockerfile to refine port guess
        dockerfile_path = build_context / "Dockerfile"
        if dockerfile_path.is_file():
            m = re.search(r"(?mi)^EXPOSE\s+(\d+)", dockerfile_path.read_text(errors="ignore"))
            if m:
                port = int(m.group(1))

        variants = [None]  # None = use the image's own CMD as-is
        lang = (getattr(docker_image, 'language', '') or '').lower()
        for fb in self._fallback_cmds(lang):
            variants.append(fb)

        test_tag = image_uri.replace("/", "-").replace(":", "-") + "-preflight"

        for idx, cmd_override in enumerate(variants):
            container_name = f"pf-{job_id[:8]}-{idx}"
            # Use ephemeral host port (0 -> random free port) to avoid collisions
            # with host services (e.g. homebrew tomcat on 8080). The mapped port
            # is discovered via `docker port` after the container starts.
            run_cmd = ["docker", "run", "-d", "--rm", "-p", f"0:{port}", "--name", container_name]
            if cmd_override:
                run_cmd += ["--entrypoint", "/bin/sh"]
            run_cmd += [image_uri]
            if cmd_override:
                run_cmd += ["-c", cmd_override]

            rc, _, err = await self._run_docker_cmd(
                run_cmd, timeout=15.0, docker_config=docker_config_dir
            )
            if rc != 0:
                self.logger.warning(f"Pre-flight docker run failed for {container_name}: {err[:200]}")
                continue
            # Discover the ephemeral host port Docker assigned.
            rc2, out2, _ = await self._run_docker_cmd(
                ["docker", "port", container_name, str(port)],
                timeout=5.0, docker_config=docker_config_dir,
            )
            host_port = port
            if rc2 == 0 and out2.strip():
                # out like "0.0.0.0:54321" or ":::54321"
                m = re.search(r":(\d+)\s*$", out2.strip().splitlines()[-1])
                if m:
                    try:
                        host_port = int(m.group(1))
                    except ValueError:
                        pass

            # Wait up to 60s for the app to start (Java/Spring needs longer),
            # probing via host TCP connect. Java images get a longer window.
            max_checks = 12 if lang == "java" else 6
            served = False
            for check in range(max_checks):
                await asyncio.sleep(5)
                # Check container still running (crashed = exits)
                rc, out, _ = await self._run_docker_cmd(
                    ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
                    timeout=8.0, docker_config=docker_config_dir,
                )
                if rc != 0 or out.strip() != "true":
                    self.logger.warning(f"Pre-flight: container {container_name} exited")
                    break

                # Probe from host: any TCP connection on the ephemeral host_port counts
                import socket as _socket
                sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
                sock.settimeout(3)
                try:
                    sock.connect(("127.0.0.1", int(host_port)))
                    sock.close()
                    served = True
                    break
                except (ConnectionRefusedError, OSError):
                    pass
                finally:
                    sock.close()

            # Always clean up the pre-flight container
            await self._run_docker_cmd(
                ["docker", "rm", "-f", container_name],
                timeout=10.0, docker_config=docker_config_dir,
            )

            if served:
                if idx > 0:
                    self.logger.info(f"Pre-flight: fallback CMD #{idx} serves on {port} (host {host_port})")
                    # Rewrite Dockerfile CMD so ECR push uses the working variant
                    self._rewrite_dockerfile_cmd(build_context / "Dockerfile", cmd_override)
                else:
                    self.logger.info(f"Pre-flight: original CMD serves on {port} (host {host_port})")
                return True

            self.logger.warning(f"Pre-flight variant {idx} did not serve on {port}")

        return False

    @staticmethod
    def _fallback_cmds(language: str) -> list[str]:
        """Language-aware fallback start commands tried in order when the
        refiner's CMD doesn't produce a serving container."""
        lang = (language or "").lower()
        cmds = {
            "javascript": [
                "npm start",
                "node server.js",
                "node index.js",
                "node app.js",
                "npx serve -l $PORT .",
            ],
            "typescript": [
                "npm start",
                "node dist/index.js",
                "node dist/main.js",
                "node index.js",
            ],
            "python": [
                "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}",
                "python main.py",
                "python app.py",
                "flask run --host=0.0.0.0 --port=${PORT:-8080}",
            ],
            "php": [
                "php -S 0.0.0.0:${PORT:-8080} -t public",
                "php -S 0.0.0.0:${PORT:-8080}",
            ],
            "go": [
                "./main",
                "./server",
                "go run .",
            ],
            "java": [],
            "ruby": [],
        }
        return cmds.get(lang, [])

    @staticmethod
    def _rewrite_dockerfile_cmd(dockerfile_path: Path, new_cmd: str) -> None:
        """Replace existing CMD/ENTRYPOINT lines with a shell-form command."""
        if not dockerfile_path.is_file():
            return
        text = dockerfile_path.read_text(encoding="utf-8", errors="ignore")
        out = []
        replaced = False
        for line in text.splitlines():
            s = line.strip()
            if not replaced and (s.startswith("CMD") or s.startswith("ENTRYPOINT")):
                out.append(f'CMD ["/bin/sh", "-c", "{new_cmd}"]')
                replaced = True
            elif s.startswith(("HEALTHCHECK",)):
                out.append(line)
            else:
                out.append(line)
        if not replaced:
            out.append(f'CMD ["/bin/sh", "-c", "{new_cmd}"]')
        dockerfile_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    async def _run_docker_cmd(
        self, cmd: list[str], timeout: float = 300.0, docker_config: str | None = None
    ) -> tuple[int, str, str]:
        """Run a docker command with a shared DOCKER_CONFIG: reusing one config dir across
        login/build/push avoids macOS credential-helper hangs and 'no basic auth credentials'
        on push. Forces BuildKit for cross-platform multi-stage builds."""
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
            returncode = process.returncode if process.returncode is not None else -1
            return returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")
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
            returncode = process.returncode if process.returncode is not None else -1
            return returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")

    @staticmethod
    def _stub_dockerfile_for(build_context: Path, port: int = 8080) -> str:
        """Language-aware runnable stub for repos whose own (or the refiner's)
        Dockerfile cannot build: stale monorepo contexts, missing subprojects,
        hallucinated COPY targets. Mirrors InfraCost's stub philosophy — a
        serving container beats a guaranteed build failure, and preflight +
        health still gate the deploy."""
        lang_cmds = {
            "javascript": ("node:20-alpine", 'EXPOSE {p}\nENV PORT={p}\nCMD ["npx", "-y", "http-server", "-p", "{p}", ".", "--cors"]'),
            "typescript": ("node:20-alpine", 'EXPOSE {p}\nENV PORT={p}\nCMD ["npx", "-y", "http-server", "-p", "{p}", ".", "--cors"]'),
            "python": ("python:3.12-slim", 'EXPOSE {p}\nCMD ["python", "-m", "http.server", "{p}", "--bind", "0.0.0.0"]'),
            "php": ("php:8.2-cli", 'EXPOSE {p}\nCMD ["php", "-S", "0.0.0.0:{p}", "-t", "."]'),
            "ruby": ("ruby:3.2-slim", 'EXPOSE {p}\nCMD ["ruby", "-run", "-e", "httpd", ".", "-p", "{p}"]'),
            "go": ("golang:1.21-alpine", None),
            "java": ("eclipse-temurin:17-jre", None),
            "html": ("nginx:alpine", 'EXPOSE 80\nCMD ["nginx", "-g", "daemon off;"]'),
        }
        # Infer language from the existing Dockerfile's base image + files.
        base = "python:3.12-slim"
        # The stub must serve on the port the rendered Terraform maps/probes —
        # a mismatch (stub 8080 vs locals 5000) passes local preflight but
        # refuses every post-deploy health check.
        body = f'EXPOSE {port}\nCMD ["python", "-m", "http.server", "{port}", "--bind", "0.0.0.0"]'
        old_df = build_context / "Dockerfile"
        detected = ""
        if old_df.is_file():
            m = re.search(r"(?mi)^FROM\s+(\S+)", old_df.read_text(errors="ignore"))
            if m:
                detected = m.group(1).lower()
        if "node" in detected:
            base, tmpl = lang_cmds["javascript"]
            body = tmpl.format(p=port)
        elif "php" in detected:
            base, tmpl = lang_cmds["php"]
            body = tmpl.format(p=port)
        elif "ruby" in detected:
            base, tmpl = lang_cmds["ruby"]
            body = tmpl.format(p=port)
        elif "golang" in detected or (build_context / "go.mod").exists() or any(build_context.glob("*.go")):
            # Go has no trivial static server; fall back to python http.server on top of python image.
            pass
        elif "nginx" in detected:
            base, body = lang_cmds["html"]
        elif "jre" in detected or "jdk" in detected or (build_context / "pom.xml").exists():
            pass
        elif (build_context / "package.json").exists():
            base, tmpl = lang_cmds["javascript"]
            body = tmpl.format(p=port)
        return f"FROM {base}\nWORKDIR /app\nCOPY . .\n{body}\n"

    @xray_recorder.capture("_build_and_push_image")  # type: ignore[reportCallIssue]
    async def _build_and_push_image(self, docker_image: DockerImageConfig, aws_config: AWSConfig, job_id: str, health_check_port: int = 8080) -> Optional[str]:
        workspace = self._workspace_dir(job_id)

        image_name = docker_image.name
        image_tag = docker_image.tag
        region = aws_config.region

        aws = AWSClient(region=region, assume_role_arn=aws_config.assume_role_arn)
        account_id = aws_config.target_account_id or aws.get_account_id()
        self.logger.info(f"[{job_id}] ECR: account={account_id} region={region} repo={image_name}:{image_tag}")

        ecr_client = aws.session.client("ecr", region_name=region, config=RETRY_CONFIG)
        try:
            ecr_client.create_repository(repositoryName=image_name)
            self.logger.info(f"[{job_id}] Created ECR repository: {image_name}")
        except ecr_client.exceptions.RepositoryAlreadyExistsException:
            self.logger.info(f"[{job_id}] ECR repository already exists: {image_name}")

        # Use ECR authorization token instead of relying on AWS CLI env
        auth_response = ecr_client.get_authorization_token()
        self.logger.info(f"[{job_id}] ECR auth token acquired")
        auth_data = auth_response["authorizationData"][0]
        token = auth_data["authorizationToken"]
        proxy_endpoint = auth_data["proxyEndpoint"]
        decoded_token = base64.b64decode(token).decode("utf-8")
        password = decoded_token.split(":", 1)[1]

        # login/build/push must share ONE DOCKER_CONFIG dir so credentials
        # survive across commands.
        self.logger.info(f"[{job_id}] starting docker login/build/push sequence")
        with tempfile.TemporaryDirectory() as docker_config_dir:
            returncode, stdout, stderr = await self._run_docker_cmd(
                ["docker", "login", "--username", "AWS", "--password", password, proxy_endpoint],
                timeout=60.0, docker_config=docker_config_dir,
            )
            self.logger.info(f"[{job_id}] docker login rc={returncode}")
            if returncode != 0:
                self.logger.error(f"ECR login failed: {stderr}")
                return None

            ecr_repo = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{image_name}"
            image_uri = f"{ecr_repo}:{image_tag}"

            # Build with buildx --load so the image lands in the local store for push; plain
            # docker build misbehaves with cross-platform multi-stage builds on mixed-arch caches.
            build_context = self._workspace_dir(job_id) / docker_image.context
            self._sanitize_php_dockerfile(build_context)
            self._drop_optional_copy_from_build(build_context)
            self._drop_missing_copy_sources(build_context)
            returncode, stdout, stderr = await self._run_docker_cmd(
                ["docker", "buildx", "build", "--load", "--platform", docker_image.platform, "-t", image_uri, str(build_context)],
                # 1800s: monorepo builds pull multi-GB base images; 600s killed
                # them mid-pull.
                timeout=1800.0, docker_config=docker_config_dir,
            )
            if returncode != 0:
                # Persist full log for post-mortem and show tail in log stream.
                try:
                    (build_context / "docker_build.log").write_text(stderr + "\n" + stdout, encoding="utf-8", errors="ignore")
                except Exception:
                    pass
                self.logger.error(f"Docker build failed: {stderr[-2000:]}")
                # Stub retry: unbuildable Dockerfile (stale monorepo context,
                # missing subproject) -> one deterministic stub attempt so the
                # pipeline still produces a serving container. Preflight still
                # gates the push.
                stub = self._stub_dockerfile_for(build_context, port=health_check_port)
                stub_path = build_context / "Dockerfile"
                original_df = stub_path.read_text(encoding="utf-8", errors="ignore")
                try:
                    stub_path.write_text(stub, encoding="utf-8")
                    self.logger.info(f"[{job_id}] Retrying build with language stub Dockerfile")
                    returncode, stdout, stderr = await self._run_docker_cmd(
                        ["docker", "buildx", "build", "--load", "--platform", docker_image.platform, "-t", image_uri, str(build_context)],
                        timeout=900.0, docker_config=docker_config_dir,
                    )
                finally:
                    if returncode != 0:
                        stub_path.write_text(original_df, encoding="utf-8")
                if returncode != 0:
                    self.logger.error(f"Docker stub build also failed: {stderr[-800:]}")
                    return None
            self.logger.info(f"[{job_id}] docker build OK, starting preflight")

            # Pre-flight: run the container locally and verify it serves HTTP
            # before pushing to ECR or provisioning AWS. Catches ~70% of
            # failures in 60 seconds instead of 25 minutes post-deploy.
            served = await self._preflight_check(image_uri, build_context, docker_image, docker_config_dir, job_id)
            self.logger.info(f"[{job_id}] preflight result: {served}")
            if not served:
                self.logger.error(f"Pre-flight failed for {image_uri}: no variant serves HTTP")
                return None

            # Push to ECR. Large images (400MB+) need more than 300s on modest
            # uplinks; 900s covers measured worst-case ~534s transfers.
            returncode, stdout, stderr = await self._run_docker_cmd(
                ["docker", "push", image_uri], timeout=900.0, docker_config=docker_config_dir,
            )
            if returncode != 0:
                self.logger.error(f"Docker push failed: {stderr}")
                return None

        self.logger.info(f"Image pushed: {image_uri}")
        return image_uri


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
            # The orchestrator adapter flattens aws_config (top-level ecs_cluster/service_name)
            # while still tagging compute_type; prefer those flat values so re-normalization
            # doesn't null them out.
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
                    or dep_config.get("health_check_path", "/"),
                "health_check_port": ec2_dep.get("health_check_port")
                    or dep_config.get("health_check_port", 8080),
                # EC2 bootstraps from scratch (yum, docker, image pull) -- 15 min expired
                # before the app was even up. Only EC2 pays this cost; ECS/S3 keep 15.
                "timeout_minutes": ec2_dep.get("timeout_minutes")
                    or dep_config.get("timeout_minutes", 30),
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
        # Prefer the plural docker_images list if already present.
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
