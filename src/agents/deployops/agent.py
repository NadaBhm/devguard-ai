"""

receive json from prev agent after user approval, 
must : terraform file, 
 


"""
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

from src.lib.aws.client import AWSClient
from src.lib.terraform.runner import TerraformRunner

logging.basicConfig(level=logging.INFO)


class DeployOpsAgent:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    def deploy(self):
        """use tf runner funcs"""
        clean_payload = self.sanitize_and_validate(self.payload)
        if not clean_payload["approval"]["deploy_approved"]:
            self.logger.warning("Deployment not approved by user")
            return {"status": "failed", "error": "deployment not approved"}
        
        job_id = clean_payload["job_id"]
        self.logger.info(f"Starting deployment for job {job_id}")
        
        workspace_dir = Path(f"/tmp/deployops/{job_id}")
        workspace_dir.mkdir(parents=True, exist_ok=True)
        
        self._write_artifacts(clean_payload["artifacts"], workspace_dir)
        
        tf_runner = TerraformRunner(workspace_dir)
        if not tf_runner.init() :
            self.logger.error("Terraform init failed")
            return {"status": "failed", "error": "terraform init failed"}
        
        plan = tf_runner.plan()
        if not plan:
            self.logger.error("Terraform plan failed")
            return {"status": "failed", "error": "terraform plan failed"}
        docker_image_uri = self._build_and_push_image(clean_payload["artifacts"], clean_payload["aws_config"])
        
        if not docker_image_uri:
            self.logger.error("Docker image build/push failed")
            return {"status": "failed", "error": "docker image build/push failed"}
        if not tf_runner.apply():
            self.logger.error("Terraform apply failed")
            return {"status": "failed", "error": "terraform apply failed"}
        output = tf_runner.output()
        self.logger.info(f"Deployment successful for job {job_id}")
        deployed_url = output.get("service_url", {}).get("value")
        
        if not self.health_check(deployed_url):
            self.rollback(clean_payload["job_id"], clean_payload)
            self.logger.error("Health check failed after deployment")
            return {"status": "failed", "error": "health check failed"}
        
        return {
        "status": "success",
        "job_id": job_id,
        "deployed_url": deployed_url,
        "resources": output
    }

        
    
    
        
        
        
        
        
        
    
        
    def status(self):
        """used to check current agent status for enhanced user experience"""
        
    def rollback(self, job_id: str, payload: dict) -> dict:
        """Rollback ECS service to previous task definition"""
        
        aws = AWSClient()
        cluster = payload["aws_config"]["ecs_cluster"]
        service_name = payload["aws_config"]["service_name"]
        
        try:
            service = aws.ecs().describe_services(
                cluster=cluster,
                services=[service_name]
            )
            
            deployments = service["services"][0].get("deployments", [])
            
            if len(deployments) < 2:
                return {
                    "status": "failed", 
                    "error": "No previous deployment to rollback to"
                }
            
            previous_task = deployments[-2]
            task_arn = previous_task["taskDefinition"]
            
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




        
    def health_check(self, url: str, max_retries: int = 3, timeout: int = 20) -> bool:
        """
        Check if deployed service is healthy.
        Returns True if /health returns 200 OK within max_retries.
        """
        if not url:
            self.logger.error("No URL provided for health check")
            return False
        
        if not url.startswith("http"):
            url = f"http://{url}"
        
        # Remove trailing slash if present
        url = url.rstrip("/")
        health_url = f"{url}/health"
        
        self.logger.info(f"Starting health check for {health_url}")
        
        for attempt in range(1, max_retries + 1):
            try:
                response = httpx.get(health_url, timeout=timeout)
                if response.status_code == 200:
                    self.logger.info(f"Health check passed on attempt {attempt}")
                    return True
                else:
                    self.logger.warning(f"Health check attempt {attempt}: status {response.status_code}")
            except httpx.TimeoutException:
                self.logger.warning(f"Health check attempt {attempt}: timeout")
            except httpx.ConnectError:
                self.logger.warning(f"Health check attempt {attempt}: connection refused")
            except Exception as e:
                self.logger.warning(f"Health check attempt {attempt}: {e}")
            
            # Wait before retry
            time.sleep(3)
        
        self.logger.error(f"Health check failed after {max_retries} attempts")
        return False
        
            
    def _write_artifacts(self, artifacts: dict, workspace: Path) -> None:
        """Write terraform files and dockerfile to workspace"""
        
        # Write terraform files
        tf_dir = workspace / "terraform"
        tf_dir.mkdir(parents=True, exist_ok=True)
        
        for filename, content in artifacts["terraform"]["files"].items():
            filepath = tf_dir / filename
            filepath.write_text(content)
            self.logger.info(f"Wrote {filename} to {filepath}")
        
        # Write Dockerfile
        dockerfile_path = workspace / "Dockerfile"
        dockerfile_path.write_text(artifacts["dockerfile"])
        self.logger.info(f"Wrote Dockerfile to {dockerfile_path}")
        
        # Write terraform variables if provided
        if artifacts["terraform"].get("variables"):
            vars_path = tf_dir / "terraform.tfvars.json"
            vars_path.write_text(json.dumps(artifacts["terraform"]["variables"], indent=2))
            self.logger.info(f"Wrote variables to {vars_path}")


    def _build_and_push_image(self, artifacts: dict, aws_config: dict) -> Optional[str]:
        """Build Docker image and push to ECR"""
        
        job_id = artifacts.get("job_id", "unknown")
        workspace = Path(f"/tmp/deployops/{job_id}")
        
        image_name = artifacts["docker_image"]["name"]
        image_tag = artifacts["docker_image"]["tag"]
        region = aws_config["region"]
        
        # Get AWS account ID
        aws = AWSClient(region=region)
        account_id = aws.get_account_id()
        
        # ECR repository URI
        ecr_repo = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{image_name}"
        image_uri = f"{ecr_repo}:{image_tag}"
        
        # Login to ECR
        login_cmd = f"aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{region}.amazonaws.com"
        result = subprocess.run(login_cmd, shell=True, capture_output=True, text=True)
        if result.returncode != 0:
            self.logger.error(f"ECR login failed: {result.stderr}")
            return None
        
        # Build Docker image
        build_cmd = ["docker", "build", "-t", image_uri, str(workspace)]
        result = subprocess.run(build_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            self.logger.error(f"Docker build failed: {result.stderr}")
            return None
        
        # Push to ECR
        push_cmd = ["docker", "push", image_uri]
        result = subprocess.run(push_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            self.logger.error(f"Docker push failed: {result.stderr}")
            return None
        
        self.logger.info(f"Image pushed: {image_uri}")
        return image_uri


    def sanitize_and_validate(self, payload: Dict[str, Any]) -> Dict[str, Any]:         
        """
        Validate and sanitize incoming payload. will return a cleaned payload with defaults for missing optional fields.
        """
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
            # Values can be string, number, bool, list, dict
            if not isinstance(value, (str, int, float, bool, list, dict)):
                raise ValueError(f"Invalid type for variable {key}")
        
        # 2c. Dockerfile (required)
        dockerfile = artifacts.get("dockerfile")
        if not dockerfile or not isinstance(dockerfile, str):
            raise ValueError("artifacts.dockerfile is required and must be a string")
        # Sanitize? Dockerfile contents can contain special chars, no need to filter.
        
        # 2d. Docker image (optional)
        docker_image = artifacts.get("docker_image", {})
        if not isinstance(docker_image, dict):
            raise ValueError("artifacts.docker_image must be a dict")
        image_name = docker_image.get("name")
        if image_name and not isinstance(image_name, str):
            raise ValueError("docker_image.name must be a string")
        image_tag = docker_image.get("tag", "latest")
        if not isinstance(image_tag, str):
            raise ValueError("docker_image.tag must be a string")
        
        # 2e. Source code path (optional)
        source_code = artifacts.get("source_code")
        if source_code is not None:
            if not isinstance(source_code, str):
                raise ValueError("artifacts.source_code must be a string")
            # Ensure it's a safe path (relative)
            source_path = Path(source_code)
            if not source_path.is_absolute():
                # If relative, resolve against a safe base? For now, just reject traversal.
                if ".." in source_code.split("/"):
                    raise ValueError("source_code cannot contain '..'")
        
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
                "dockerfile": dockerfile,
                "docker_image": {
                    "name": image_name or "devguard-app",
                    "tag": image_tag
                },
                "source_code": source_code
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
        
        
    
    