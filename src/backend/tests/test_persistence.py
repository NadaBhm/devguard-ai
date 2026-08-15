import os

os.environ["DATABASE_URL"] = "sqlite://"

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from src.backend import models  # noqa: E402
from src.backend.persistence import (  # noqa: E402
    derive_results_from_state,
    persist_results,
    serialize_state,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    yield session()


def _make_state(terraform_shape: dict) -> dict:
    return {
        "status": "completed",
        "infracost_result": {
            "generated_terraform": terraform_shape,
            "cost_estimate": {"breakdown": [{"service": "ECS Fargate", "monthly_cost_usd": 89.5}]},
        },
    }


def test_persist_dockerfile_from_real_deploy_inputs(db: Session) -> None:
    """The real InfraCost agent stashes Dockerfile + image identity under
    _deploy_inputs.artifacts; those must reach the artifacts tab too."""
    state = _make_state(
        {
            "files": {
                "main.tf": "resource \"aws_ecs_cluster\" \"this\" {}",
                "variables.tf": "variable \"region\" {}",
                "outputs.tf": "output \"url\" {}",
            },
            "variables": {"region": "us-east-1"},
        }
    )
    state["infracost_result"]["_deploy_inputs"] = {
        "compute_type": "ecs",
        "artifacts": {
            "terraform": {
                "files": {"main.tf": "x"},
                "variables": {"region": "us-east-1"},
            },
            "dockerfile": "FROM python:3.12-slim\nCOPY . /app\n",
            "docker_image": {"name": "devguard-app", "tag": "sha-a1b2c3d"},
            "source_code": ".",
        },
    }
    persist_results(db, "test-run-docker", state)

    artifacts = db.query(models.TerraformArtifact).filter_by(run_id="test-run-docker").all()
    by_path = {a.file_path: a for a in artifacts}
    assert by_path["Dockerfile"].artifact_type == "dockerfile"
    assert by_path["Dockerfile"].content == "FROM python:3.12-slim\nCOPY . /app\n"
    assert by_path["docker-image.json"].artifact_type == "docker-image"
    assert "devguard-app" in by_path["docker-image.json"].content
    assert "sha-a1b2c3d" in by_path["docker-image.json"].content


def test_persist_dockerfile_from_mock_deployops_artifacts(db: Session) -> None:
    """Mock/legacy runs carry Docker artifacts on deployops_result.artifacts;
    those must be persisted too."""
    state = _make_state(
        {
            "main_tf": "resource \"aws_ecs_cluster\" \"mock\" {}",
            "variables_tf": "variable \"region\" {}",
            "outputs_tf": "output \"url\" {}",
        }
    )
    state["deployops_result"] = {
        "deployment_status": "success",
        "artifacts": {
            "dockerfile": "FROM python:3.12\nCOPY . /app\nCMD [\"python\", \"app.py\"]",
            "docker_image": {"name": "devguard-app", "tag": "latest"},
        },
    }
    persist_results(db, "test-run-mock-docker", state)

    artifacts = db.query(models.TerraformArtifact).filter_by(run_id="test-run-mock-docker").all()
    by_path = {a.file_path: a for a in artifacts}
    assert by_path["Dockerfile"].content.startswith("FROM python:3.12")
    assert by_path["docker-image.json"].content == '{\n  "name": "devguard-app",\n  "tag": "latest"\n}'


def test_persist_lambda_run_has_no_docker_artifacts(db: Session) -> None:
    """A serverless Lambda run has no container artifacts; persistence must be
    a silent no-op, not an error."""
    state = _make_state(
        {
            "files": {
                "main.tf": "resource \"aws_lambda_function\" \"this\" {}",
                "variables.tf": "variable \"region\" {}",
                "outputs.tf": "output \"arn\" {}",
            },
            "variables": {"region": "us-east-1"},
        }
    )
    state["infracost_result"]["_deploy_inputs"] = {
        "compute_type": "lambda",
        "artifacts": {
            "terraform": {"files": {"main.tf": "x"}, "variables": {}},
            "dockerfile": None,
            "docker_image": {},
            "source_code": ".",
        },
    }
    persist_results(db, "test-run-lambda", state)

    artifacts = db.query(models.TerraformArtifact).filter_by(run_id="test-run-lambda").all()
    assert "Dockerfile" not in {a.file_path for a in artifacts}
    assert "docker-image.json" not in {a.file_path for a in artifacts}
    assert {a.file_path for a in artifacts} == {"main.tf", "variables.tf", "outputs.tf"}


def test_persist_results_handles_nested_real_agent_shape(db: Session) -> None:
    """The real InfraCost agent stores files nested under `files`
    (generated_terraform.files.main.tf); persist_results must unwrap that
    so the Terraform tab actually gets artifacts for real runs."""
    state = _make_state(
        {
            "files": {
                "main.tf": "resource \"aws_ecs_cluster\" \"this\" {}",
                "variables.tf": "variable \"region\" {}",
                "outputs.tf": "output \"url\" {}",
            },
            "variables": {"region": "us-east-1"},
        }
    )
    written = persist_results(db, "test-run-nested", state)

    artifacts = db.query(models.TerraformArtifact).filter_by(run_id="test-run-nested").all()
    assert written >= 3
    assert {a.file_path for a in artifacts} == {"main.tf", "variables.tf", "outputs.tf"}
    assert any(a.content.startswith("resource \"aws_ecs_cluster\"") for a in artifacts)


def test_persist_results_handles_flat_mock_shape(db: Session) -> None:
    """The orchestrator's mock emits flat keys (generated_terraform.main_tf);
    that legacy shape must keep working too."""
    state = _make_state(
        {
            "main_tf": "resource \"aws_ecs_cluster\" \"mock\" {}",
            "variables_tf": "variable \"region\" {}",
            "outputs_tf": "output \"url\" {}",
        }
    )
    persist_results(db, "test-run-flat", state)

    artifacts = db.query(models.TerraformArtifact).filter_by(run_id="test-run-flat").all()
    assert {a.file_path for a in artifacts} == {"main.tf", "variables.tf", "outputs.tf"}
    assert any(a.content.startswith("resource \"aws_ecs_cluster\" \"mock\"") for a in artifacts)


def test_derive_results_from_state_serves_live_gate_paused_run() -> None:
    """While a run is paused at a human gate the normalized tables are empty;
    the results endpoint must derive the same rows from run_metadata so the
    UI can render CodeSec + Terraform tabs before the run is terminal."""
    state = {
        "job_id": "test-live",
        "status": "infra_generating",
        "codesec_result": {
            "sast_findings": [
                {
                    "file": "app/db.py",
                    "line": 24,
                    "tool": "semgrep",
                    "severity": "critical",
                    "rule_id": "python.sql-injection",
                    "message": "Possible SQL injection",
                    "remediation": "Use parameterized queries",
                }
            ]
        },
        "infracost_result": {
            "generated_terraform": {
                "files": {
                    "main.tf": "resource \"aws_ecs_cluster\" \"live\" {}",
                    "variables.tf": "variable \"region\" {}",
                    "outputs.tf": "output \"url\" {}",
                },
                "variables": {"region": "us-east-1"},
            },
            "cost_estimate": {"breakdown": [{"service": "ECS Fargate", "monthly_cost_usd": 89.5}]},
        },
    }

    derived = derive_results_from_state(state)

    assert [f["rule_id"] for f in derived["codesec_findings"]] == ["python.sql-injection"]
    assert derived["codesec_findings"][0]["file_path"] == "app/db.py"
    assert {a["file_path"] for a in derived["terraform_artifacts"]} == {
        "main.tf", "variables.tf", "outputs.tf"
    }
    assert derived["infracost_estimates"][0]["monthly_cost_usd"] == 89.5


def test_derive_results_from_state_falls_back_to_total_when_breakdown_empty() -> None:
    """The real InfraCost agent returns a single monthly_cost_usd with an
    empty breakdown; the estimate must not collapse to $0 in the UI."""
    state = {
        "job_id": "test-live-total",
        "status": "infra_generating",
        "infracost_result": {
            "generated_terraform": {
                "files": {"main.tf": "resource \"x\" {}"},
                "variables": {"region": "us-east-1"},
            },
            "cost_estimate": {
                "currency": "USD",
                "breakdown": [],
                "range_min": 11.53,
                "range_max": 17.3,
                "monthly_cost_usd": 14.42,
            },
        },
    }

    derived = derive_results_from_state(state)

    assert len(derived["infracost_estimates"]) == 1
    assert derived["infracost_estimates"][0]["monthly_cost_usd"] == 14.42
    assert derived["infracost_estimates"][0]["resource_name"] == "Estimated total"


def test_derive_results_from_state_includes_docker_artifacts() -> None:
    """The live path (gate-paused runs) must surface Docker artifacts from
    _deploy_inputs just like the terminal persistence path does."""
    state = {
        "job_id": "test-live-docker",
        "status": "awaiting_approval_gate_2",
        "infracost_result": {
            "generated_terraform": {
                "files": {
                    "main.tf": "resource \"aws_ecs_cluster\" \"live\" {}",
                    "variables.tf": "variable \"region\" {}",
                    "outputs.tf": "output \"url\" {}",
                },
                "variables": {"region": "us-east-1"},
            },
            "cost_estimate": {"monthly_cost_usd": 14.42, "breakdown": []},
            "_deploy_inputs": {
                "compute_type": "ecs",
                "artifacts": {
                    "terraform": {"files": {"main.tf": "x"}, "variables": {}},
                    "dockerfile": "FROM python:3.11-slim\nCOPY . /app\n",
                    "docker_image": {"name": "devguard-app", "tag": "sha-live"},
                },
            },
        },
    }

    derived = derive_results_from_state(state)

    by_path = {a["file_path"]: a for a in derived["terraform_artifacts"]}
    assert by_path["Dockerfile"]["artifact_type"] == "dockerfile"
    assert by_path["Dockerfile"]["content"] == "FROM python:3.11-slim\nCOPY . /app\n"
    assert by_path["docker-image.json"]["artifact_type"] == "docker-image"
    assert "sha-live" in by_path["docker-image.json"]["content"]


def test_persist_results_normalizes_severities_for_db_constraint(db: Session) -> None:
    """Real semgrep/gitleaks severities (error/warning/info) violate the
    ck_codesec_findings_severity constraint; persistence must map them into
    the allowed set instead of failing mid-commit."""
    state = _make_state({"main_tf": "resource \"x\" {}"})
    state["codesec_result"] = {
        "sast_findings": [
            {
                "file": "a.py", "line": 1, "tool": "semgrep",
                "severity": "error", "rule_id": "E1", "message": "x",
            },
            {
                "file": "b.py", "line": 2, "tool": "semgrep",
                "severity": "warning", "rule_id": "W1", "message": "y",
            },
            {
                "file": "c.py", "line": 3, "tool": "semgrep",
                "severity": "info", "rule_id": "I1", "message": "z",
            },
            {
                "file": "d.py", "line": 4, "tool": "semgrep",
                "rule_id": "M1", "message": "m",
            },
        ]
    }
    persist_results(db, "test-run-sev", state)

    rows = db.query(models.CodeSecFinding).filter_by(run_id="test-run-sev").all()
    by_rule = {r.rule_id: r for r in rows}
    assert by_rule["E1"].severity == "high"
    assert by_rule["W1"].severity == "medium"
    assert by_rule["I1"].severity == "low"
    assert by_rule["M1"].severity == "low"


def test_persist_results_materializes_secrets(db: Session) -> None:
    """Secret findings (gitleaks/builtin) previously vanished on completion;
    they must be persisted as CodeSecFinding rows."""
    state = _make_state({"main_tf": "resource \"x\" {}"})
    state["codesec_result"] = {
        "secrets": [
            {
                "type": "aws_access_key_id",
                "tool": "gitleaks",
                "file": "src/config.py",
                "line": 12,
                "severity": "high",
                "value_preview": "AKIA***",
                "remediation": "Rotate the key",
            },
            {
                "type": "generic_api_key",
                "tool": "gitleaks",
                "file": "src/client.js",
                "line": 8,
                "severity": "info",
                "value_preview": "sk-***",
                "remediation": "Move to a secret manager",
            },
        ]
    }
    written = persist_results(db, "test-run-secrets", state)

    rows = db.query(models.CodeSecFinding).filter_by(run_id="test-run-secrets").all()
    assert written >= len(rows)
    assert {r.scanner for r in rows} == {"gitleaks"}
    by_file = {r.file_path: r for r in rows}
    assert by_file["src/config.py"].rule_id == "aws_access_key_id"
    assert by_file["src/config.py"].severity == "high"
    assert by_file["src/client.js"].severity == "low"
    assert by_file["src/client.js"].description == "sk-***"


def test_persist_results_normalizes_regex_fallback_scanner(db: Session) -> None:
    """The built-in secrets scanner emits tool='regex-fallback', which violates
    the ck_codesec_findings_scanner constraint. It must be persisted as
    'gitleaks' so the commit does not fail."""
    state = _make_state({"main_tf": "resource \"x\" {}"})
    state["codesec_result"] = {
        "secrets": [
            {
                "type": "generic_api_key",
                "tool": "regex-fallback",
                "file": "main.py",
                "line": 52,
                "severity": "high",
                "value_preview": "***",
                "remediation": "Remove hardcoded secrets",
            },
        ]
    }
    written = persist_results(db, "test-run-regex-scanner", state)

    assert written >= 1
    rows = db.query(models.CodeSecFinding).filter_by(run_id="test-run-regex-scanner").all()
    assert len(rows) == 1
    assert rows[0].scanner == "gitleaks"
    assert rows[0].rule_id == "generic_api_key"


class _FakeInterrupt:
    """Stand-in for langgraph.types.Interrupt (value dict, not a dict itself)."""

    def __init__(self, value):
        self.value = value


def test_serialize_state_preserves_gate_interrupt():
    state = {
        "status": "awaiting_approval_gate_2",
        "job_id": "run-1",
        "__interrupt__": [
            _FakeInterrupt({
                "gate": "gate_2_pre_deployops",
                "message": "Review cost",
                "context": {"iteration": 1, "max_iterations": 3, "monthly_cost_usd": 145.32},
            })
        ],
        "__private_scratch": "must not leak",
    }

    out = serialize_state(state)

    assert out["__interrupt__"][0]["value"]["gate"] == "gate_2_pre_deployops"
    assert out["__interrupt__"][0]["value"]["context"]["iteration"] == 1
    assert out["__interrupt__"][0]["value"]["context"]["monthly_cost_usd"] == 145.32
    assert "__private_scratch" not in out
