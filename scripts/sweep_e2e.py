"""
E2E sweep harness: run a corpus of real repos through the full DevGuard
pipeline (real InfraCost LLM + real AWS DeployOps) and measure the outcome.

Usage:
    python -m scripts.sweep_e2e --corpus repos.json
    python -m scripts.sweep_e2e --corpus repos.json --teardown

Corpus format (JSON list):
    [{"repo_url": "...", "branch": "main", "commit_sha": "HEAD", "label": "name"}, ...]

Flow per repo:
    1. POST /api/jobs/          -> returns job_id + first gate
    2. Approve gate 1 (background curl -- approve blocks until next gate)
    3. Approve gate 2 (background curl)
    4. Poll until terminal status (completed / rolled_back / failed / rejected)
    5. Record outcome. Health check + deployment_status come from the final state.

The sweep runs repos one at a time (the backend drives the pipeline in-process,
so parallel jobs would serialize anyway) and prints a CSV-style summary at the end.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib import request, error

BASE_URL = "http://127.0.0.1:8000"
TOKEN_PATH = Path("/tmp/devguard-token")
POLL_INTERVAL = 30  # seconds
GATE_1_TIMEOUT = 300  # codesec + repo ingest
GATE_2_TIMEOUT = 3600  # real InfraCost LLM (big monorepos can take 30min+)
TERMINAL_TIMEOUT = 2400  # full DeployOps (build/push/apply/health)
TERMINAL_STATUSES = {"completed", "rolled_back", "failed", "rejected"}

_LOGIN_BODY = json.dumps(
    {
        "email": "deploy-test@devguard.ai",
        "password": "DeployTest2026!",
    }
).encode()


def _refresh_token() -> None:
    req = request.Request(
        f"{BASE_URL}/api/auth/login",
        data=_LOGIN_BODY,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
    TOKEN_PATH.write_text(data["access_token"])
    print("  (token refreshed)", flush=True)


def _auth_headers() -> dict[str, str]:
    token = TOKEN_PATH.read_text().strip()
    return {"Authorization": f"Bearer {token}"}


def _http_json(method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={**_auth_headers(), "Content-Type": "application/json"},
        method=method,
    )
    try:
        with request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except error.HTTPError as exc:
        if exc.code == 401:
            _refresh_token()
            req = request.Request(
                f"{BASE_URL}{path}",
                data=body,
                headers={**_auth_headers(), "Content-Type": "application/json"},
                method=method,
            )
            with request.urlopen(req) as resp:
                return json.loads(resp.read().decode())
        raise


def _status(job_id: str) -> dict:
    try:
        return _http_json("GET", f"/api/jobs/{job_id}")
    except error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise


def _wait_for_gate(job_id: str, gate_name: str, timeout: int) -> dict | None:
    """Poll until the given gate exists or the run goes terminal."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _status(job_id)
        if not state:
            time.sleep(POLL_INTERVAL)
            continue
        orchestrator = state.get("orchestrator_status") or ""
        if orchestrator in TERMINAL_STATUSES:
            return None
        gate = state.get("gate")
        if gate == gate_name:
            return state
        time.sleep(POLL_INTERVAL)
    return None


def _approve_background(job_id: str, approved: bool = True) -> None:
    """Approve in a background curl: the approve endpoint blocks until the next
    gate, so it must not be called in-process from this harness."""
    payload = json.dumps({"approved": approved, "comment": "", "request_regeneration": False})
    token = TOKEN_PATH.read_text().strip()
    cmd = (
        f"curl -s -X POST {BASE_URL}/api/jobs/{job_id}/approve "
        f"-H 'Authorization: Bearer {token}' -H 'Content-Type: application/json' "
        f"-d '{payload}' > /tmp/sweep-approve-{job_id}.log 2>&1 &"
    )
    subprocess.run(cmd, shell=True, check=False)


def _terminal_state(job_id: str, timeout: int) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = _status(job_id)
        if not state:
            time.sleep(POLL_INTERVAL)
            continue
        orchestrator = state.get("orchestrator_status") or ""
        if orchestrator in TERMINAL_STATUSES:
            return state
        time.sleep(POLL_INTERVAL)
    return None


def run_one(repo: dict, plan_only: bool = False) -> dict:
    label = repo.get("label") or repo["repo_url"]
    print(f"\n=== {label} ===", flush=True)

    try:
        created = _http_json(
            "POST",
            "/api/jobs/",
            {
                "repo_url": repo["repo_url"],
                "default_branch": repo.get("branch", "main"),
                "commit_sha": repo.get("commit_sha", "HEAD"),
            },
        )
    except error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        return {"label": label, "repo": repo["repo_url"], "outcome": f"create_failed: {detail}"}
    job_id = created.get("job_id")
    print(f"job_id: {job_id} | status: {created.get('orchestrator_status')}", flush=True)
    if not job_id:
        return {"label": label, "repo": repo["repo_url"], "outcome": "no_job_id"}

    # Gate 1 (codesec + repo ingest) -> approve.
    gate1 = _wait_for_gate(job_id, "gate_1_pre_infracost", GATE_1_TIMEOUT)
    if gate1 is None:
        state = _terminal_state(job_id, 60) or {}
        return {"label": label, "repo": repo["repo_url"], "outcome": state.get("orchestrator_status", "unknown"), "error": state.get("error")}
    print("gate 1 reached -> approving", flush=True)
    _approve_background(job_id, True)

    # Gate 2 (real InfraCost LLM) -> approve.
    gate2 = _wait_for_gate(job_id, "gate_2_pre_deployops", GATE_2_TIMEOUT)
    if gate2 is None:
        state = _terminal_state(job_id, 60) or {}
        return {"label": label, "repo": repo["repo_url"], "outcome": state.get("orchestrator_status", "unknown"), "error": state.get("error")}
    warnings = (gate2.get("state") or {}).get("infracost_result", {}).get("warnings") or gate2.get("warnings") or []
    if plan_only:
        print("  plan-only: stopping before DeployOps", flush=True)
        return {"job_id": job_id, "label": label, "repo": repo["repo_url"], "outcome": "plan_only", "warnings": warnings}
    print("gate 2 reached -> approving", flush=True)
    if warnings:
        print(f"  gate 2 warnings: {warnings}", flush=True)
    _approve_background(job_id, True)

    # Wait for the run to finish (DeployOps runs synchronously in the approve).
    final = _terminal_state(job_id, TERMINAL_TIMEOUT)
    if final is None:
        return {"label": label, "repo": repo["repo_url"], "outcome": "timeout"}
    # The deployops_result is nested under state (the top-level field mirrors
    # it only after the persistence flush); read both so the summary isn't
    # fooled into reporting a successful deploy as deployment_status=None.
    state = final.get("state") or {}
    deployment = (
        final.get("deployment_status")
        or (final.get("deployops_result") or {}).get("deployment_status")
        or (state.get("deployops_result") or {}).get("deployment_status")
    )
    health = (
        final.get("health_check")
        or (final.get("deployops_result") or {}).get("health_check")
        or (state.get("deployops_result") or {}).get("health_check")
    )
    # The orchestrator can mark a run terminal a beat before the deployops
    # result is persisted to the job doc — re-fetch briefly to capture it.
    outcome = final.get("orchestrator_status")
    if outcome == "completed" and not deployment:
        for _ in range(15):
            time.sleep(3)
            late = _status(job_id)
            late_state = late.get("state") or {}
            deployment = (
                (late.get("deployops_result") or {}).get("deployment_status")
                or (late_state.get("deployops_result") or {}).get("deployment_status")
            )
            health = (
                (late.get("deployops_result") or {}).get("health_check")
                or (late_state.get("deployops_result") or {}).get("health_check")
            )
            if deployment:
                break
    error = final.get("error") or (final.get("state") or {}).get("error")
    if not error:
        err_log = ((final.get("state") or {}).get("error_log") or [])
        if err_log:
            error = err_log[-1].get("message", "")[:200]
    print(f"  outcome: {outcome} | deployment: {deployment} | error: {error}", flush=True)
    return {
        "job_id": job_id,
        "label": label,
        "repo": repo["repo_url"],
        "outcome": outcome,
        "deployment_status": deployment,
        "health": health,
        "error": error,
        "warnings": warnings,
    }


def _destroy(job_id: str) -> None:
    """Destroy a job's AWS resources via the API (service_name jobs) or
    direct `terraform destroy` (EC2-shaped jobs with local tfstate), falling
    back to S3 bucket cleanup for static/pre-apply jobs.
    """
    try:
        state = _status(job_id)
        s = state.get("state") or {}
        svc = (state.get("deployops_result") or {}).get("terraform_outputs", {}).get("service_name") or \
              (s.get("deployops_result") or {}).get("terraform_outputs", {}).get("service_name", "")
        if svc:
            resp = _http_json("POST", f"/api/jobs/{job_id}/destroy", {"confirm_service_name": svc})
            status = (resp or {}).get("status", "unknown")
            if status == "destroyed":
                print(f"  destroyed {svc}", flush=True)
            else:
                print(f"  destroy returned '{status}' for {svc} (may still be live)", flush=True)
        elif Path(f"/tmp/deployops/{job_id}/terraform/terraform.tfstate").exists():
            # EC2-shaped: no service_name output, but live resources behind
            # a local tfstate. Terraform needs creds from .env.
            print("  no service_name (EC2-shaped) — direct terraform destroy", flush=True)
            tf_env = dict(os.environ)
            env_path = Path(__file__).resolve().parent.parent / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, _, v = line.partition("=")
                        tf_env.setdefault(k.strip(), v.strip())
            proc = subprocess.run(
                ["terraform", "-chdir=/tmp/deployops/" + job_id + "/terraform",
                 "destroy", "-auto-approve", "-input=false"],
                capture_output=True, text=True, timeout=1200,
                env=tf_env,
            )
            ok = "Destroy complete" in (proc.stdout or "")
            print(f"    {'destroyed' if ok else 'rc=' + str(proc.returncode)}", flush=True)
        else:
            print("  no service_name (s3/static or pre-apply) — direct bucket cleanup", flush=True)
            _destroy_static_bucket(job_id)
    except Exception as exc:
        print(f"  destroy failed for {job_id}: {exc} — falling back to rm -rf", flush=True)
        subprocess.run(f"rm -rf /tmp/deployops/{job_id}", shell=True, check=False)


def _destroy_static_bucket(job_id: str) -> None:
    """Best-effort removal of the job's S3 static bucket + objects (no
    service_name exists to drive the /destroy endpoint, which 400s for S3)."""
    try:
        from src.lib.aws.client import AWSClient

        aws = AWSClient(region="us-east-1")
        s3 = aws.session.client("s3")
        bucket = f"devguard-static-{job_id[:32].lower()}"
        objs = s3.list_objects_v2(Bucket=bucket).get("Contents", [])
        for o in objs:
            s3.delete_object(Bucket=bucket, Key=o["Key"])
        s3.delete_bucket(Bucket=bucket)
        print(f"  removed s3 bucket {bucket} ({len(objs)} objects)", flush=True)
    except Exception as exc:
        print(f"  static bucket cleanup skipped: {exc}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, help="JSON file: list of {repo_url, branch, commit_sha, label}")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N repos")
    parser.add_argument("--teardown", action="store_true", help="Terraform-destroy deploy workspaces after each run")
    parser.add_argument("--plan-only", action="store_true", help="Stop after InfraCost (gate 2) and run terraform plan dry-run without apply")
    parser.add_argument("--max-cost-usd", type=float, default=None, help="Abort sweep if estimated monthly cost exceeds this (saves budget)")
    parser.add_argument("--no-skip-validated", action="store_true", help="Re-test repos already validated (default: skip them)")
    args = parser.parse_args()

    repos = json.loads(Path(args.corpus).read_text(encoding="utf-8"))
    if args.limit:
        repos = repos[: args.limit]

    if not args.no_skip_validated:
        vpath = Path(__file__).resolve().parent / "validated.json"
        done = set(json.loads(vpath.read_text())["validated"]) if vpath.exists() else set()
        before = len(repos)
        repos = [r for r in repos if r.get("label") not in done]
        skipped = before - len(repos)
        if skipped:
            print(f"skip-list: {skipped} already-validated repo(s) skipped", flush=True)
        if not repos:
            print("nothing to do — all repos validated", flush=True)
            return

    results = []
    total_cost = 0.0
    for repo in repos:
        result = run_one(repo, plan_only=args.plan_only)
        # Cost guard: sum estimated monthly cost from infracost_result if available
        try:
            state = _status(result.get("job_id", ""))
            s = state.get("state") or {}
            cost = (s.get("infracost_result", {}).get("estimated_monthly_cost", {}).get("amount") or 0)
            total_cost += float(cost)
            if args.max_cost_usd is not None and total_cost > args.max_cost_usd:
                print(f"Cost guard tripped: ${total_cost:.2f} > ${args.max_cost_usd:.2f} — aborting sweep", flush=True)
                break
        except Exception:
            pass
        results.append(result)
        if result.get("outcome") == "completed" and result.get("job_id"):
            vpath = Path(__file__).resolve().parent / "validated.json"
            try:
                vdata = json.loads(vpath.read_text()) if vpath.exists() else {"validated": []}
                label = result.get("label")
                if label and label not in vdata["validated"]:
                    vdata["validated"].append(label)
                    vpath.write_text(json.dumps(vdata, indent=2))
            except Exception:
                pass
        if args.teardown:
            job_id = result.get("job_id")
            if job_id:
                _destroy(job_id)

    print("\n\n=== SWEEP SUMMARY ===")
    print("repo,outcome,deployment_status,health_status,error")
    for r in results:
        health = ""
        if r.get("health"):
            health = str(r["health"].get("passed") or r["health"].get("status") or r["health"])
        print(f"{r.get('label','')},{r.get('outcome','')},{r.get('deployment_status','')},{health},{r.get('error','')}")
    good = sum(1 for r in results if r.get("outcome") == "completed" and r.get("deployment_status") == "success")
    print(f"\n{good}/{len(results)} fully successful deploys")


if __name__ == "__main__":
    main()