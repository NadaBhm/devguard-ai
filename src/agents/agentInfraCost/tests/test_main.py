"""Tests for the mock API (main.py) — POST /agents/infracost/generate."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from main import app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
client = TestClient(app)


def _load_raw(filename: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Nominal cases — the 4 fixtures, expecting different results per context
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected_compute_type",
    [
        ("sample_input.json", "ecs"),
        ("sample_input_variant_lambda_candidate.json", "lambda"),
        ("sample_input_variant_node_ecs.json", "ecs"),
    ],
)
def test_generate_returns_expected_compute_type(
    filename: str, expected_compute_type: str
) -> None:
    response = client.post("/agents/infracost/generate", json=_load_raw(filename))
    assert response.status_code == 200
    body = response.json()
    assert body["compute_type"] == expected_compute_type
    assert body["schema_version"] == "1.1"
    assert body["approval"] == {"status": "pending", "approved_by": None}


def test_generate_ecs_response_shape_matches_contract() -> None:
    response = client.post("/agents/infracost/generate", json=_load_raw("sample_input.json"))
    body = response.json()
    assert body["aws_config"]["ecs"] is not None
    assert body["aws_config"]["lambda"] is None
    assert body["aws_config"]["ec2"] is None
    assert body["deployment_config"]["ecs"] is not None
    assert body["artifacts"]["dockerfile"] is not None
    assert body["artifacts"]["docker_image"]["tag"] == "sha-a1b2c3d"


def test_generate_lambda_response_has_no_docker_fields_when_uncontainerized() -> None:
    response = client.post(
        "/agents/infracost/generate",
        json=_load_raw("sample_input_variant_lambda_candidate.json"),
    )
    body = response.json()
    assert body["artifacts"]["dockerfile"] is None
    assert body["artifacts"]["docker_image"] is None
    assert body["aws_config"]["lambda"]["memory_mb"] > 0


# --------------------------------------------------------------------------
# Limit / edge cases
# --------------------------------------------------------------------------


def test_generate_missing_commit_sha_falls_back_to_latest_tag(caplog) -> None:
    raw = _load_raw("sample_input.json")
    raw["repo_metadata"]["commit_sha"] = ""
    response = client.post("/agents/infracost/generate", json=raw)
    assert response.status_code == 200
    assert response.json()["artifacts"]["docker_image"]["tag"] == "latest"


def test_generate_enrichment_is_always_fallback_for_this_mock() -> None:
    response = client.post(
        "/agents/infracost/generate", json=_load_raw("sample_input_variant_node_ecs.json")
    )
    assert response.json()["enrichment"]["enrichment_source"] == "fallback"


# --------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------


def test_generate_rejects_low_confidence_with_422() -> None:
    response = client.post(
        "/agents/infracost/generate",
        json=_load_raw("sample_input_variant_low_confidence.json"),
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["error"] == "low_confidence"
    assert detail["job_id"] == "job-variant-003"


def test_generate_rejects_wrong_status_with_422() -> None:
    raw = _load_raw("sample_input.json")
    raw["status"] = "processing"
    response = client.post("/agents/infracost/generate", json=raw)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_status"


def test_generate_rejects_missing_stack_detection_with_422() -> None:
    raw = _load_raw("sample_input.json")
    del raw["stack_detection"]
    response = client.post("/agents/infracost/generate", json=raw)
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "missing_stack_detection"
