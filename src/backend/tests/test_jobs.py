import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB = Path(__file__).parent / "test_jobs.db"

os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB}"
os.environ["DEVGUARD_REPORT_DIR"] = str(Path(__file__).parent / "test_reports")

import src.backend.main as main  # noqa: E402

TEST_USER = {
    "email": "tester@devguard.ai",
    "password": "s3cret-pass",
    "first_name": "T",
    "last_name": "Ester",
}


@pytest.fixture(scope="module", autouse=True)
def _clean_db():
    TEST_DB.unlink(missing_ok=True)
    report_dir = Path(__file__).parent / "test_reports"
    with TestClient(main.app) as client:
        yield client
    TEST_DB.unlink(missing_ok=True)
    shutil.rmtree(report_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def auth_headers(_clean_db):
    client = _clean_db
    r = client.post("/api/auth/register", json=TEST_USER)
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json=TEST_USER)
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_full_pipeline_through_api(_clean_db, auth_headers):
    headers = auth_headers
    client = _clean_db
    r = client.post(
        "/api/jobs/",
        headers=headers,
        json={"repo_url": "https://github.com/NadaBhm/devguard-ai", "commit_sha": "x"},
    )
    assert r.status_code in (200, 201)
    body = r.json()
    job_id = body["job_id"]
    assert body["orchestrator_status"] == "analyzing"
    assert body["gate"] == "gate_1_pre_infracost"

    r = client.post(
        f"/api/jobs/{job_id}/approve",
        headers=headers,
        json={"approved": True, "approved_by": "test@devguard.ai"},
    )
    body = r.json()
    assert body["gate"] == "gate_2_pre_deployops"

    r = client.post(
        f"/api/jobs/{job_id}/approve",
        headers=headers,
        json={"approved": True, "approved_by": "test@devguard.ai"},
    )
    body = r.json()
    assert body["orchestrator_status"] == "completed"
    assert body["gate"] is None

    r = client.get(f"/api/jobs/{job_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["orchestrator_status"] == "completed"


def test_jobs_require_auth(_clean_db):
    client = _clean_db
    r = client.get("/api/jobs/")
    assert r.status_code == 401
    r = client.post(
        "/api/jobs/",
        json={"repo_url": "https://github.com/NadaBhm/devguard-ai", "commit_sha": "x"},
    )
    assert r.status_code == 401


def test_tenant_isolation_blocks_cross_user_access(_clean_db, auth_headers):
    client = _clean_db
    r = client.post(
        "/api/auth/register",
        json={
            "email": "intruder@devguard.ai",
            "password": "x",
            "first_name": "I",
            "last_name": "N",
        },
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", json={"email": "intruder@devguard.ai", "password": "x"})
    assert r.status_code == 200, r.text
    intruder = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post(
        "/api/jobs/",
        headers=auth_headers,
        json={"repo_url": "https://github.com/NadaBhm/tenant-check", "commit_sha": "x"},
    )
    assert r.status_code in (200, 201)
    job_id = r.json()["job_id"]

    assert client.get(f"/api/jobs/{job_id}", headers=intruder).status_code == 404
    assert (
        client.post(
            f"/api/jobs/{job_id}/approve",
            headers=intruder,
            json={"approved": True, "approved_by": "intruder@devguard.ai"},
        ).status_code
        == 404
    )
    assert client.get(f"/api/jobs/{job_id}/results", headers=intruder).status_code == 404

    listed = client.get("/api/jobs/", headers=intruder).json()["jobs"]
    assert all(j["job_id"] != job_id for j in listed)
    deploys = client.get("/api/deployments/", headers=intruder).json()["deployments"]
    assert all(d["run_id"] != job_id for d in deploys)

    assert client.get(f"/api/jobs/{job_id}", headers=auth_headers).status_code == 200


def test_run_persisted_to_schema_tables(_clean_db, auth_headers):
    con = sqlite3.connect(TEST_DB)
    con.row_factory = sqlite3.Row
    runs = con.execute("select id, status, run_metadata from analysis_runs").fetchall()
    assert len(runs) >= 1
    run = next(r for r in runs if r["status"] == "completed")
    assert run["status"] == "completed"
    md = json.loads(run["run_metadata"])
    assert md.get("deployops_result", {}).get("deployment_status") == "success"
    assert con.execute("select count(*) from projects").fetchone()[0] >= 1
    assert con.execute("select count(*) from deployments").fetchone()[0] >= 1
    con.close()


def test_results_endpoint_returns_normalized_tables(_clean_db, auth_headers):
    headers = auth_headers
    client = _clean_db
    r = client.post(
        "/api/jobs/",
        headers=headers,
        json={"repo_url": "https://github.com/NadaBhm/devguard-ai", "commit_sha": "x"},
    )
    job_id = r.json()["job_id"]
    client.post(
        f"/api/jobs/{job_id}/approve",
        headers=headers,
        json={"approved": True, "approved_by": "x@y.z"},
    )
    r = client.post(
        f"/api/jobs/{job_id}/approve",
        headers=headers,
        json={"approved": True, "approved_by": "x@y.z"},
    )
    assert r.json()["orchestrator_status"] == "completed"

    res = client.get(f"/api/jobs/{job_id}/results", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["job_id"] == job_id
    assert len(data["agent_tasks"]) == 3
    assert len(data["deployments"]) == 1
    for row in data["infracost_estimates"]:
        assert "monthly_cost_usd" in row
    assert client.get("/api/jobs/does-not-exist/results", headers=headers).status_code == 404


def test_register_creates_verified_user(_clean_db):
    client = _clean_db
    r = client.post(
        "/api/auth/register",
        json={"email": "fresh@devguard.ai", "password": "x", "first_name": "A", "last_name": "B"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_verified"] is True

    login = client.post("/api/auth/login", json={"email": "fresh@devguard.ai", "password": "x"})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200


def test_refresh_endpoint_accepts_json_body(_clean_db, auth_headers):
    client = _clean_db
    r = client.post("/api/auth/login", json=TEST_USER)
    assert r.status_code == 200, r.text
    refresh = r.json()["refresh_token"]

    r = client.post("/api/auth/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "access_token" in body
    assert "refresh_token" in body

    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200


def test_refresh_requires_json_body(_clean_db):
    client = _clean_db
    assert client.post("/api/auth/refresh").status_code == 422


def test_refresh_rejects_access_token(_clean_db):
    client = _clean_db
    r = client.post("/api/auth/login", json=TEST_USER)
    access = r.json()["access_token"]
    r = client.post("/api/auth/refresh", json={"refresh_token": access})
    assert r.status_code == 401


def test_refresh_token_cannot_be_used_as_access_token(_clean_db):
    client = _clean_db
    r = client.post("/api/auth/login", json=TEST_USER)
    refresh = r.json()["refresh_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert me.status_code == 401


def _run_job_to_completion(client, headers):
    r = client.post(
        "/api/jobs/",
        headers=headers,
        json={"repo_url": "https://github.com/NadaBhm/devguard-ai", "commit_sha": "x"},
    )
    job_id = r.json()["job_id"]
    approve = {"approved": True, "approved_by": "x@y.z"}
    client.post(f"/api/jobs/{job_id}/approve", headers=headers, json=approve)
    r = client.post(f"/api/jobs/{job_id}/approve", headers=headers, json=approve)
    assert r.json()["orchestrator_status"] == "completed"
    return job_id


def test_report_and_sbom_download_endpoints(_clean_db, auth_headers):
    headers = auth_headers
    client = _clean_db
    job_id = _run_job_to_completion(client, headers)

    sb = client.get(f"/api/jobs/{job_id}/sbom/download", headers=headers)
    assert sb.status_code == 200
    assert "components" in sb.json()
    assert "download_url" not in sb.json()

    rep = client.get(f"/api/jobs/{job_id}/report/download", headers=headers)
    assert rep.status_code == 200
    assert "text/html" in rep.headers["content-type"]

    stored = client.get(f"/api/jobs/{job_id}", headers=headers).json()
    assert stored["state"].get("job_id") == job_id
    assert stored["state"]["final_report"]["download_url"] == f"/api/jobs/{job_id}/report/download"

    assert client.get(f"/api/jobs/{job_id}/sbom/download").status_code == 401


def test_per_node_progress_is_streamed(_clean_db, auth_headers, monkeypatch):
    headers = auth_headers
    client = _clean_db
    published = []

    def fake_publish(job_id, phase, progress, message=""):
        published.append((phase, progress))
        return True

    monkeypatch.setattr("src.backend.api.jobs.publish_progress", fake_publish)
    r = client.post(
        "/api/jobs/",
        headers=headers,
        json={"repo_url": "https://github.com/NadaBhm/devguard-ai", "commit_sha": "x"},
    )
    assert r.status_code in (200, 201)

    phases = {phase for phase, _ in published}
    assert "codesec_agent" in phases
    assert "human_gate_1" in phases


def test_multi_container_dockerfile_edits_route_by_context():
    from src.backend.api.jobs import ArtifactEditRequest, _apply_artifact_edits
    from src.backend.artifact_validation import allowed_file_path

    assert allowed_file_path("backend/Dockerfile") is True
    assert allowed_file_path("frontend/Dockerfile") is True
    assert allowed_file_path("Dockerfile") is True
    assert allowed_file_path("../evil/Dockerfile") is False
    assert allowed_file_path("nested/path/to/Dockerfile") is True

    state = {
        "infracost_result": {"_deploy_inputs": {"artifacts": {
            "docker_images": [
                {"name": "devguard-app", "dockerfile": "OLD1", "context": "."},
                {"name": "devguard-app-frontend", "dockerfile": "OLD2", "context": "frontend"},
            ]
        }}}
    }
    _apply_artifact_edits(state, [
        ArtifactEditRequest(file_path="Dockerfile", content="NEW1"),
        ArtifactEditRequest(file_path="frontend/Dockerfile", content="NEW2"),
    ])
    images = state["infracost_result"]["_deploy_inputs"]["artifacts"]["docker_images"]
    assert images[0]["dockerfile"] == "NEW1"
    assert images[1]["dockerfile"] == "NEW2"
