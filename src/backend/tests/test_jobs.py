import json
import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB = Path(__file__).parent / "test_jobs.db"

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"

import src.backend.main as main  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _clean_db():
    TEST_DB.unlink(missing_ok=True)
    with TestClient(main.app) as client:
        yield client
    TEST_DB.unlink(missing_ok=True)


def test_full_pipeline_through_api(_clean_db):
    client = _clean_db
    r = client.post(
        "/api/jobs/",
        json={"repo_url": "https://github.com/NadaBhm/devguard-ai", "commit_sha": "x"},
    )
    assert r.status_code in (200, 201)
    body = r.json()
    job_id = body["job_id"]
    assert body["orchestrator_status"] == "analyzing"
    assert body["gate"] == "gate_1_pre_infracost"

    r = client.post(
        f"/api/jobs/{job_id}/approve",
        json={"approved": True, "approved_by": "test@devguard.ai"},
    )
    body = r.json()
    assert body["gate"] == "gate_2_pre_deployops"

    r = client.post(
        f"/api/jobs/{job_id}/approve",
        json={"approved": True, "approved_by": "test@devguard.ai"},
    )
    body = r.json()
    assert body["orchestrator_status"] == "completed"
    assert body["gate"] is None

    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json()["orchestrator_status"] == "completed"


def test_run_persisted_to_schema_tables(_clean_db):
    con = sqlite3.connect(TEST_DB)
    con.row_factory = sqlite3.Row
    runs = con.execute("select id, status, run_metadata from analysis_runs").fetchall()
    assert len(runs) >= 1
    run = runs[-1]
    assert run["status"] == "completed"
    md = json.loads(run["run_metadata"])
    assert md.get("deployops_result", {}).get("deployment_status") == "success"
    assert con.execute("select count(*) from projects").fetchone()[0] >= 1
    assert con.execute("select count(*) from deployments").fetchone()[0] == 1
    con.close()


def test_results_endpoint_returns_normalized_tables(_clean_db):
    client = _clean_db
    r = client.post(
        "/api/jobs/",
        json={"repo_url": "https://github.com/NadaBhm/devguard-ai", "commit_sha": "x"},
    )
    job_id = r.json()["job_id"]
    client.post(
        f"/api/jobs/{job_id}/approve", json={"approved": True, "approved_by": "x@y.z"}
    )
    r = client.post(
        f"/api/jobs/{job_id}/approve", json={"approved": True, "approved_by": "x@y.z"}
    )
    assert r.json()["orchestrator_status"] == "completed"

    res = client.get(f"/api/jobs/{job_id}/results")
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == job_id
    assert len(data["agent_tasks"]) == 3
    assert len(data["deployments"]) == 1
    # Every row has its uuid PK rendered as a string and the money as numbers.
    for row in data["infracost_estimates"]:
        assert "monthly_cost_usd" in row
    assert client.get("/api/jobs/does-not-exist/results").status_code == 404
