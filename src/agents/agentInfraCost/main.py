"""FastAPI entry point for the InfraCost Agent — Sprint 1 mock API.

Exposes ``POST /agents/infracost/generate``. The request body is Agent 1's
raw analysis payload; it is genuinely validated (module 1,
``input_validator``) and a genuine architecture decision is made (module 2,
``decision_engine``) — those two modules are real and tested. Everything
downstream of that (Terraform, real cost, real FinOps advice, real LLM
enrichment) does not exist yet (modules 3-10), so this endpoint fills those
parts of the response with clearly-labelled placeholder values, just to
guarantee the response always has the exact shape of the final contract
(``models.output_schema.InfraCostOutput``). Other teams can already
integrate against this real shape today, before the rest of the pipeline is
built.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Body, FastAPI, HTTPException

from core.decision_engine import DecisionResult, decide_architecture
from core.input_validator import (
    InputValidationError,
    InvalidStatusError,
    LowConfidenceError,
    MalformedInputError,
    MissingStackDetectionError,
    validate_input,
)
from models.input_schema import RepoAnalysisInput
from models.output_schema import (
    Approval,
    Artifacts,
    AwsConfigEc2,
    AwsConfigEcs,
    AwsConfigLambda,
    DeploymentConfigEc2,
    DeploymentConfigEcs,
    DeploymentConfigLambda,
    DockerImage,
    Ec2AwsConfig,
    Ec2DeploymentConfig,
    Ec2InfraCostOutput,
    EcsAwsConfig,
    EcsDeploymentConfig,
    EcsInfraCostOutput,
    Enrichment,
    InfraCostOutput,
    LambdaAwsConfig,
    LambdaDeploymentConfig,
    LambdaInfraCostOutput,
    Money,
    TerraformArtifacts,
    TerraformFiles,
)

logger = logging.getLogger(__name__)

app = FastAPI(title="InfraCost Agent (mock)")

_ERROR_CODES: dict[type[InputValidationError], str] = {
    InvalidStatusError: "invalid_status",
    MissingStackDetectionError: "missing_stack_detection",
    MalformedInputError: "malformed_input",
    LowConfidenceError: "low_confidence",
}

_NOT_BUILT_YET = (
    "Mock response: modules 3-10 (terraform, real cost, finops, llm "
    "enrichment) are not built yet."
)
_MOCK_COST = Money(amount=0.0, currency="USD", range_min=0.0, range_max=0.0)


def _mock_terraform_files() -> TerraformFiles:
    """Placeholder for module 3 (terraform_generator), not built yet."""
    placeholder = f"# TODO: {_NOT_BUILT_YET}\n"
    return TerraformFiles.model_validate(
        {
            "main.tf": placeholder,
            "variables.tf": placeholder,
            "outputs.tf": placeholder,
        }
    )


def _mock_artifacts(analysis: RepoAnalysisInput, decision: DecisionResult) -> Artifacts:
    """Build the artifacts block; the docker fields follow the contract's
    'commit_sha if available, else "latest" + warning' rule.
    """
    terraform = TerraformArtifacts(
        files=_mock_terraform_files(),
        variables={"region": "us-east-1", "environment": "dev"},
    )
    source_code = f"/tmp/repo_{analysis.job_id}"

    is_lambda_zip = (
        decision.compute_type == "lambda"
        and not analysis.stack_detection.container.detected
    )
    if is_lambda_zip:
        return Artifacts(
            terraform=terraform, dockerfile=None, docker_image=None, source_code=source_code
        )

    commit_sha = analysis.repo_metadata.commit_sha
    if commit_sha:
        tag = f"sha-{commit_sha[:7]}"
    else:
        tag = "latest"
        logger.warning(
            "job_id=%s has no commit_sha; falling back to docker tag 'latest'",
            analysis.job_id,
        )
    base_image = analysis.stack_detection.container.base_image or "python:3.12-slim"
    return Artifacts(
        terraform=terraform,
        dockerfile=f"FROM {base_image}\nCOPY . /app\n",
        docker_image=DockerImage(name="devguard-app", tag=tag),
        source_code=source_code,
    )


def _mock_enrichment() -> Enrichment:
    return Enrichment(
        architecture_explanation=_NOT_BUILT_YET,
        cost_summary=_NOT_BUILT_YET,
        finops_justification=_NOT_BUILT_YET,
        enrichment_source="fallback",
    )


def _build_ecs_output(
    analysis: RepoAnalysisInput, decision: DecisionResult, artifacts: Artifacts
) -> EcsInfraCostOutput:
    sizing = decision.sizing
    return EcsInfraCostOutput(
        job_id=analysis.job_id,
        artifacts=artifacts,
        aws_config=AwsConfigEcs(
            region="us-east-1",
            estimated_monthly_cost=_MOCK_COST,
            ecs=EcsAwsConfig(
                cluster="devguard-cluster",
                service_name="app-service",
                task_cpu=str(sizing["task_cpu"]),
                task_memory=str(sizing["task_memory"]),
            ),
        ),
        deployment_config=DeploymentConfigEcs(
            ecs=EcsDeploymentConfig(
                strategy="rolling",
                health_check_path="/health",
                health_check_port=8080,
                timeout_minutes=5,
                min_healthy_percent=50,
                max_percent=200,
            )
        ),
        approval=Approval(status="pending"),
        enrichment=_mock_enrichment(),
    )


def _build_lambda_output(
    analysis: RepoAnalysisInput, decision: DecisionResult, artifacts: Artifacts
) -> LambdaInfraCostOutput:
    sizing = decision.sizing
    return LambdaInfraCostOutput(
        job_id=analysis.job_id,
        artifacts=artifacts,
        aws_config=AwsConfigLambda(
            region="us-east-1",
            estimated_monthly_cost=_MOCK_COST,
            lambda_=LambdaAwsConfig(
                function_name="app-handler",
                runtime="python3.12",
                memory_mb=int(sizing["memory_mb"]),
                timeout_seconds=30,
                handler="handler.main",
            ),
        ),
        deployment_config=DeploymentConfigLambda(
            lambda_=LambdaDeploymentConfig(strategy="all_at_once", reserved_concurrency=None)
        ),
        approval=Approval(status="pending"),
        enrichment=_mock_enrichment(),
    )


def _build_ec2_output(
    analysis: RepoAnalysisInput, decision: DecisionResult, artifacts: Artifacts
) -> Ec2InfraCostOutput:
    sizing = decision.sizing
    return Ec2InfraCostOutput(
        job_id=analysis.job_id,
        artifacts=artifacts,
        aws_config=AwsConfigEc2(
            region="us-east-1",
            estimated_monthly_cost=_MOCK_COST,
            ec2=Ec2AwsConfig(
                instance_type=str(sizing["instance_type"]),
                ami_id="ami-0000000000000000",
                instance_count=1,
                key_pair_name="devguard-key",
            ),
        ),
        deployment_config=DeploymentConfigEc2(
            ec2=Ec2DeploymentConfig(
                strategy="rolling",
                health_check_path="/health",
                health_check_port=8080,
                timeout_minutes=5,
            )
        ),
        approval=Approval(status="pending"),
        enrichment=_mock_enrichment(),
    )


_BUILDERS = {
    "ecs": _build_ecs_output,
    "lambda": _build_lambda_output,
    "ec2": _build_ec2_output,
}


def build_mock_response(analysis: RepoAnalysisInput, decision: DecisionResult) -> InfraCostOutput:
    """Assemble the full output contract from real modules 1-2 plus mocks.

    Args:
        analysis: The validated payload (module 1's output).
        decision: The architecture decision (module 2's output).

    Returns:
        A fully-shaped ``InfraCostOutput`` — real ``compute_type`` and
        ``sizing``, placeholder everything else.
    """
    artifacts = _mock_artifacts(analysis, decision)
    builder = _BUILDERS[decision.compute_type]
    return builder(analysis, decision, artifacts)


@app.post("/agents/infracost/generate", response_model=InfraCostOutput)
def generate(raw: dict[str, Any] = Body(...)) -> InfraCostOutput:
    """Validate a repo analysis, decide an architecture, return a mocked contract.

    Raises:
        HTTPException: 422, if module 1's validation rejects the payload.
            The response body names which rule failed (``error``), why
            (``message``), and which job (``job_id``).
    """
    try:
        analysis = validate_input(raw)
    except InputValidationError as exc:
        error_code = _ERROR_CODES.get(type(exc), "invalid_input")
        raise HTTPException(
            status_code=422,
            detail={"error": error_code, "message": str(exc), "job_id": exc.job_id},
        ) from exc

    decision = decide_architecture(analysis)
    return build_mock_response(analysis, decision)
