#!/usr/bin/env python3
"""
CodeSec Agent — Quick Demo

1. Change REPO_URL / USE_SAMPLE_REPO in the config values below.
2. Run:  python src/agents/codesec_demo.py

Uses ONLY built-in scanners (no semgrep/trivy/gitleaks needed). For real
repos (USE_SAMPLE_REPO = False), git must be installed.
"""

REPO_URL = "https://github.com/Oussama928/Card-Learning-App.git"
USE_SAMPLE_REPO = False
import sys, os, tempfile, shutil, re, types
from pathlib import Path

# `src` must be on sys.path for the `agents` package to be importable.
SRC_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SRC_DIR.parent
sys.path.insert(0, str(SRC_DIR))


from agents.codesec.scanners import sast, secrets, dependencies, dockerfile_scanner, sbom
from agents.codesec.models import SASTFinding, Severity, DependenciesResult, VulnerablePackage
from agents.codesec.scanners import find_files, read_file_safe
from agents.codesec.scanners.dependencies import _parse_manifest_files
from agents.codesec.scanners.dockerfile_scanner import _run_builtin_checks
from agents.codesec.scanners.sbom import _generate_fallback_sbom
from agents.codesec.scanners.secrets import _run_regex_fallback
import agents.codesec.agent as agent_mod
from agents.codesec.agent import CodeSecAgent

# Patch scanners to builtin-only (no external tools)

def _demo_sast(repo_path):
    findings = []
    patterns = [
        (r"query\s*=\s*f[\"'].*\{.*\}", "SQL injection (f-string)", Severity.CRITICAL, "CWE-89"),
        (r"eval\s*\(", "Dangerous eval()", Severity.HIGH, "CWE-95"),
        (r"exec\s*\(", "Dangerous exec()", Severity.HIGH, "CWE-95"),
        (r"subprocess\.call.*shell\s*=\s*True", "Shell injection", Severity.HIGH, "CWE-78"),
        (r"password\s*=\s*[\"'][^\"']+[\"']", "Hardcoded password", Severity.HIGH, "CWE-798"),
        (r"api_key\s*=\s*[\"'][^\"']+[\"']", "Hardcoded API key", Severity.HIGH, "CWE-798"),
        (r"DEBUG\s*=\s*True", "Debug mode enabled", Severity.MEDIUM, "CWE-489"),
        (r"pickle\.loads", "Unsafe pickle", Severity.HIGH, "CWE-502"),
        (r"yaml\.load\s*\([^)]*\)", "Unsafe YAML load", Severity.HIGH, "CWE-502"),
    ]
    for fpath in find_files(Path(repo_path), patterns=("*.py",), exclude=("test_", "tests/", "*_test.py", "venv/", ".venv/")):
        content = read_file_safe(fpath, max_size_mb=1)
        if not content: continue
        rel = fpath.relative_to(repo_path).as_posix()
        for i, line in enumerate(content.splitlines(), 1):
            for pat, msg, sev, cwe in patterns:
                if re.search(pat, line):
                    findings.append(SASTFinding(
                        rule_id=f"builtin.{cwe}", tool="builtin-sast", severity=sev,
                        category="security", cwe_id=cwe, file=rel, line=i,
                        message=msg, snippet=line.strip()[:80],
                        remediation=f"Review and fix {cwe}"))
    return findings


def _demo_deps(repo_path):
    total, direct, transitive = _parse_manifest_files(Path(repo_path))
    vulns = []
    for rf in Path(repo_path).rglob("requirements*.txt"):
        content = rf.read_text(errors="ignore")
        if "requests==2.25.0" in content or "requests==2.25" in content:
            vulns.append(VulnerablePackage(
                package="requests", installed_version="2.25.0",
                fixed_version="2.31.0", cve_id="CVE-2023-32681",
                severity=Severity.HIGH, cvss_score=7.5,
                description="Session fixation vulnerability"))
        if "sqlalchemy==2.0.0" in content or "sqlalchemy==2.0" in content:
            vulns.append(VulnerablePackage(
                package="sqlalchemy", installed_version="2.0.0",
                fixed_version="2.0.30", cve_id="CVE-2024-XXXX",
                severity=Severity.MEDIUM, cvss_score=5.3,
                description="Potential SQL injection edge case"))
    return DependenciesResult(total_packages=total, direct=direct, transitive=transitive, vulnerable_packages=vulns)


sast.run_sast = _demo_sast
agent_mod.run_sast = _demo_sast
dependencies.run_dependency_scan = _demo_deps
agent_mod.run_dependency_scan = _demo_deps
dockerfile_scanner.run_dockerfile_scan = lambda p: _run_builtin_checks(Path(p))
agent_mod.run_dockerfile_scan = lambda p: _run_builtin_checks(Path(p))
sbom.generate_sbom = lambda p: _generate_fallback_sbom(Path(p))
agent_mod.generate_sbom = lambda p: _generate_fallback_sbom(Path(p))

# Builtin-only secrets: skip gitleaks and use regex fallback so no external tools are invoked.
secrets.run_secrets_scan = _run_regex_fallback
agent_mod.run_secrets_scan = _run_regex_fallback

def _make_sample_repo(base_dir: Path) -> Path:
    repo = base_dir / "sample_vuln_repo"
    if repo.exists(): shutil.rmtree(repo)
    repo.mkdir()
    (repo / "requirements.txt").write_text(
        "fastapi==0.110.0\nsqlalchemy==2.0.0\npsycopg2-binary==2.9.9\nrequests==2.25.0\n")
    (repo / "main.py").write_text(
        'from fastapi import FastAPI\nfrom sqlalchemy import create_engine, text\n'
        'import os\n\napp = FastAPI()\n'
        'engine = create_engine("postgresql://user:pass@localhost/db")\n\n'
        'DEBUG = True\n\n'
        '@app.get("/users/{user_id}")\n'
        'def get_user(user_id: str):\n'
        '    query = f"SELECT * FROM users WHERE id = {user_id}"\n'
        '    with engine.connect() as conn:\n'
        '        result = conn.execute(text(query))\n'
        '    return {"user": result.fetchone()}\n\n'
        '@app.post("/run")\n'
        'def run_cmd(cmd: str):\n'
        '    import subprocess\n'
        '    return subprocess.call(cmd, shell=True)\n')
    (repo / "app").mkdir()
    (repo / "app" / "db.py").write_text(
        'import os\n\npassword = "supersecret123"\n'
        'api_key = "sk-live-abc123xyz"\n\n'
        'DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/db")\n\n'
        'def get_user_raw(user_id):\n'
        '    query = f"SELECT * FROM users WHERE id = {user_id}"\n'
        '    return query\n\n'
        'def load_config(data):\n'
        '    import pickle\n'
        '    return pickle.loads(data)\n')
    (repo / ".env").write_text(
        'DATABASE_URL=postgresql://user:pass@localhost/db\n'
        'AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n'
        'AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY\n')
    (repo / "Dockerfile").write_text(
        'FROM python:latest\nWORKDIR /app\nCOPY requirements.txt .\n'
        'RUN pip install -r requirements.txt\nCOPY . .\n'
        'EXPOSE 8000\nUSER root\nENV PASSWORD=secret123\n'
        'CMD ["uvicorn", "main:app", "--host", "0.0.0.0"]\n')
    (repo / "docker-compose.yml").write_text(
        "version: '3.8'\nservices:\n  web:\n    build: .\n"
        '    ports:\n      - "8000:8000"\n  db:\n    image: postgres:15\n')
    (repo / "README.md").write_text("# Sample Vulnerable App\n")
    return repo


if __name__ == "__main__":
    try:
        import nest_asyncio  # type: ignore
        nest_asyncio.apply()  # type: ignore # allow asyncio.run() inside an existing loop (e.g. notebooks)
    except ImportError:
        pass  # optional; not needed when running as a plain script

    _orig_clone = CodeSecAgent._clone_repo
    _orig_validate = CodeSecAgent._validate_github_url

    with tempfile.TemporaryDirectory() as tmpdir:
        if USE_SAMPLE_REPO:
            repo_path = _make_sample_repo(Path(tmpdir))
            CodeSecAgent._demo_repo_path = repo_path # type: ignore
            CodeSecAgent._clone_repo = lambda self, repo_url, job_id: repo_path
            CodeSecAgent._validate_github_url = lambda self, url: (url or "")
        else:
            CodeSecAgent._validate_github_url = lambda self, url: (url or "")

        agent = CodeSecAgent(clone_dir=str(Path(tmpdir) / "clones"))

        print("=" * 70)
        print("   DevGuard AI — CodeSec Agent Demo")
        print("=" * 70)
        print(f"\nTarget: {REPO_URL}")
        print("\nRunning...\n")

        result = agent.analyze_sync(REPO_URL, job_id="demo-001")

        CodeSecAgent._clone_repo = _orig_clone
        CodeSecAgent._validate_github_url = _orig_validate

        print("\n" + "=" * 70)
        print("   RESULT")
        print("=" * 70)
        print(f"\nScore:  {result.security_score.score}/100 (Grade {result.security_score.grade.value})")
        print(f"Lang:   {result.stack_detection.primary_language}")
        print(f"Stack:  {', '.join(result.stack_detection.frameworks) or '—'}")
        print(f"DB:     {result.stack_detection.database or '—'}")
        print(f"Container: {'Yes' if result.stack_detection.container.detected else 'No'}")

        print(f"\nBreakdown:")
        print(f"  SAST: {result.security_score.breakdown.sast}/25")
        print(f"  Secrets: {result.security_score.breakdown.secrets}/20")
        print(f"  Deps: {result.security_score.breakdown.dependencies}/20")
        print(f"  Docker: {result.security_score.breakdown.dockerfile}/15")
        print(f"  SBOM: {result.security_score.breakdown.sbom}/10")
        print(f"  Stack: {result.security_score.breakdown.stack_detection}/10")

        print(f"\nFindings:  Critical={result.security_score.severity_counts.critical}, "
              f"High={result.security_score.severity_counts.high}, "
              f"Medium={result.security_score.severity_counts.medium}")
        print(f"  SAST:     {len(result.sast_findings)}")
        print(f"  Secrets:  {len(result.secrets)}")
        print(f"  VulnDeps: {len(result.dependencies.vulnerable_packages)}")
        print(f"  Docker:   {len(result.dockerfile_findings)}")
        print(f"  SBOM:     {result.sbom.components_count} components")

        if result.sast_findings:
            print("\n--- SAST ---")
            for f in result.sast_findings[:6]:
                print(f"  [{f.severity.upper():8}] {f.file}:{f.line}  {f.message}")
            if len(result.sast_findings) > 6:
                print(f"  ... +{len(result.sast_findings)-6} more")

        if result.secrets:
            print("\n--- SECRETS ---")
            for s in result.secrets[:4]:
                print(f"  [{s.severity.upper()}] {s.type} in {s.file}:{s.line}")

        if result.dependencies.vulnerable_packages:
            print("\n--- VULNERABLE DEPS ---")
            for v in result.dependencies.vulnerable_packages:
                print(f"  {v.package} {v.installed_version} -> {v.fixed_version or '?'}  ({v.cve_id})")

        if result.dockerfile_findings:
            print("\n--- DOCKERFILE ---")
            for d in result.dockerfile_findings[:4]:
                print(f"  [{d.severity.upper()}] {d.file}:{d.line}  {d.message}")

        if result.security_score.recommendations:
            print("\n--- TOP RECS ---")
            for i, r in enumerate(result.security_score.recommendations[:5], 1):
                print(f"  {i}. {r}")

        out = PROJECT_ROOT / "codesec_result.json"
        out.write_text(result.model_dump_json(indent=2), encoding="utf-8")
        print(f"\nJSON saved: {out}")
        print("=" * 70)