"""
Database Seeder Script

python -m scripts.seed                    # Default: 3 projects, 5 runs each
python -m scripts.seed --projects 5 --runs-per-project 3
python -m scripts.seed --clear            # Clear existing data first
python -m scripts.seed --only-findings    # Only seed findings for existing runs
"""

import argparse
import random
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from src.backend.database import SessionLocal, init_db
from src.backend import models



REPO_TEMPLATES = [
    {
        "name": "devguard-api",
        "url": "https://github.com/NadaBhm/devguard-api",
        "branch": "main",
        "language": "python",
        "framework": "fastapi",
    },
    {
        "name": "devguard-frontend",
        "url": "https://github.com/NadaBhm/devguard-frontend",
        "branch": "main",
        "language": "typescript",
        "framework": "nextjs",
    },
    {
        "name": "devguard-infra",
        "url": "https://github.com/NadaBhm/devguard-infra",
        "branch": "main",
        "language": "hcl",
        "framework": "terraform",
    },
    {
        "name": "payment-service",
        "url": "https://github.com/NadaBhm/payment-service",
        "branch": "develop",
        "language": "python",
        "framework": "django",
    },
    {
        "name": "notification-worker",
        "url": "https://github.com/NadaBhm/notification-worker",
        "branch": "main",
        "language": "go",
        "framework": "gin",
    },
]

SCANNERS = ["semgrep", "gitleaks", "trivy", "bandit"]
SEVERITIES = ["critical", "high", "medium", "low"]
SEVERITY_WEIGHTS = [0.05, 0.15, 0.35, 0.45]  # Most findings are low/medium

RULE_IDS = {
    "semgrep": [
        "python.sql-injection",
        "python.xss.generic",
        "python.path-traversal",
        "python.command-injection",
        "python.hardcoded-secret",
        "python.weak-crypto",
        "javascript.xss.react",
        "javascript.prototype-pollution",
    ],
    "gitleaks": [
        "aws-access-key-id",
        "aws-secret-access-key",
        "github-pat",
        "slack-token",
        "stripe-api-key",
        "generic-api-key",
        "private-key",
    ],
    "trivy": [
        "DS001",  # Running as root
        "DS002",  # Latest tag
        "DS003",  # No healthcheck
        "DS004",  # Exposed port
        "DS005",  # No user
    ],
    "bandit": [
        "B101",  # assert_used
        "B102",  # exec_used
        "B301",  # pickle
        "B303",  # md5
        "B311",  # random
        "B501",  # ssl_verify_false
        "B601",  # shell_injection
    ],
}

REMEDIATION_HINTS = {
    "python.sql-injection": "Use parameterized queries with SQLAlchemy ORM",
    "python.xss.generic": "Use DOMPurify or framework's built-in escaping",
    "python.path-traversal": "Validate and sanitize file paths; use os.path.basename",
    "python.command-injection": "Use subprocess with shell=False and argument lists",
    "python.hardcoded-secret": "Move secrets to environment variables or secret manager",
    "python.weak-crypto": "Use AES-256-GCM or ChaCha20-Poly1305 via cryptography library",
    "javascript.xss.react": "Avoid dangerouslySetInnerHTML; use safe rendering",
    "javascript.prototype-pollution": "Freeze Object.prototype; validate input keys",
    "aws-access-key-id": "Rotate key immediately; use IAM roles or AWS Secrets Manager",
    "aws-secret-access-key": "Rotate key immediately; use IAM roles or AWS Secrets Manager",
    "github-pat": "Revoke token; use fine-grained PAT or GitHub App",
    "slack-token": "Revoke token; use Slack App with granular scopes",
    "stripe-api-key": "Rotate key in Stripe dashboard; use restricted keys",
    "generic-api-key": "Rotate key; store in secret manager",
    "private-key": "Rotate key pair; use HSM or KMS",
    "DS001": "Add 'USER appuser' after creating non-root user in Dockerfile",
    "DS002": "Pin base image to specific version (e.g., python:3.12-slim)",
    "DS003": "Add HEALTHCHECK instruction to Dockerfile",
    "DS004": "Remove unnecessary EXPOSE; use firewall/security groups",
    "DS005": "Create and switch to non-root user",
    "B101": "Remove assert statements from production code",
    "B102": "Avoid exec(); use importlib or explicit imports",
    "B301": "Avoid pickle; use JSON or msgpack for serialization",
    "B303": "Use SHA-256 or SHA-3 via hashlib",
    "B311": "Use secrets module for cryptographic randomness",
    "B501": "Enable SSL verification; use certifi bundle",
    "B601": "Use subprocess with shell=False; validate input",
}

ARCHITECTURES = ["ecs_fargate", "lambda", "ec2", "hybrid"]
ENVIRONMENTS = ["dev", "staging", "prod"]
DEPLOYMENT_STATUSES = ["succeeded", "failed", "rolled_back"]
DEPLOYMENT_STATUS_WEIGHTS = [0.7, 0.2, 0.1]

RUN_STATUSES = ["completed", "failed", "rejected"]
RUN_STATUS_WEIGHTS = [0.7, 0.2, 0.1]

AGENT_NAMES = ["codesec", "infracost", "deployops"]


def random_datetime_within(days_back: int = 30) -> datetime:
    now = datetime.now(timezone.utc)
    delta = timedelta(
        days=random.randint(0, days_back),
        hours=random.randint(0, 23),
        minutes=random.randint(0, 59),
        seconds=random.randint(0, 59),
    )
    return now - delta


def weighted_choice(choices: list, weights: list):
    return random.choices(choices, weights=weights, k=1)[0]


def get_or_create_system_user(db: Session) -> models.User:
    user = db.query(models.User).filter(models.User.email == "system@devguard.ai").first()
    if user:
        return user
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")
    user = models.User(
        email="system@devguard.ai",
        hashed_password=pwd_context.hash("system-user-not-for-login"),
        first_name="System",
        last_name="User",
        is_verified=True,
        role=models.UserRole.ADMIN.value,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def clear_all_data(db: Session):
    """Delete all seeded data (in dependency order)."""
    print("Clearing existing data...")
    
    db.query(models.Notification).delete()
    db.query(models.CostAlert).delete()
    db.query(models.Deployment).delete()
    db.query(models.TerraformArtifact).delete()
    db.query(models.InfracostEstimate).delete()
    db.query(models.CodeSecFinding).delete()
    db.query(models.AgentTask).delete()
    db.query(models.AnalysisRun).delete()
    db.query(models.Project).delete()
    # Keep the system user
    db.commit()
    print("   Done.")


def seed_projects(db: Session, user: models.User, count: int) -> list[models.Project]:
    print(f"Seeding {count} projects...")
    projects = []
    
    for i in range(min(count, len(REPO_TEMPLATES))):
        template = REPO_TEMPLATES[i]
        project = db.query(models.Project).filter(
            models.Project.github_url == template["url"]
        ).first()
        
        if not project:
            project = models.Project(
                user_id=user.id,
                repo_name=template["name"],
                github_url=template["url"],
                default_branch=template["branch"],
                is_active=True,
            )
            db.add(project)
            projects.append(project)
        else:
            projects.append(project)
    
    db.commit()
    for p in projects:
        db.refresh(p)
    
    print(f"   Created {len(projects)} projects")
    return projects


def seed_runs(db: Session, projects: list[models.Project], user: models.User, runs_per_project: int) -> list[models.AnalysisRun]:
    print(f"Seeding {runs_per_project} runs per project ({len(projects)} projects)...")
    runs = []
    
    for project in projects:
        for i in range(runs_per_project):
            status = weighted_choice(RUN_STATUSES, RUN_STATUS_WEIGHTS)
            started_at = random_datetime_within(30)
            
            if status == "completed":
                completed_at = started_at + timedelta(minutes=random.randint(2, 15))
                duration = int((completed_at - started_at).total_seconds())
            elif status == "failed":
                completed_at = started_at + timedelta(minutes=random.randint(1, 10))
                duration = int((completed_at - started_at).total_seconds())
            else:  # rejected
                completed_at = started_at + timedelta(minutes=random.randint(1, 5))
                duration = int((completed_at - started_at).total_seconds())
            
            commit_sha = "".join(random.choices("0123456789abcdef", k=40))
            commit_messages = [
                "feat: add user authentication",
                "fix: resolve SQL injection vulnerability",
                "refactor: improve cost estimation logic",
                "chore: update dependencies",
                "feat: add deployment health checks",
                "fix: patch CVE-2024-xxxx in requests",
                "docs: update API documentation",
                "test: add integration tests for orchestrator",
            ]
            
            run = models.AnalysisRun(
                project_id=project.id,
                commit_sha=commit_sha,
                commit_message=random.choice(commit_messages),
                status=status,
                triggered_by=user.id,
                started_at=started_at,
                completed_at=completed_at,
                duration_seconds=duration,
                run_metadata={
                    "status": status,
                    "orchestrator_status": status,
                    "job_id": str(uuid4()),
                },
            )
            db.add(run)
            runs.append(run)
    
    db.commit()
    for r in runs:
        db.refresh(r)
    
    print(f"   Created {len(runs)} runs")
    return runs


def seed_agent_tasks(db: Session, runs: list[models.AnalysisRun]):
    print(f"Seeding agent tasks for {len(runs)} runs...")
    count = 0
    
    for run in runs:
        for agent_name in AGENT_NAMES:
            # Not all runs have all agents (example: failed early)
            if run.status == "failed" and agent_name != "codesec":
                continue
            if run.status == "rejected" and agent_name in ["infracost", "deployops"]:
                continue
            
            task = models.AgentTask(
                run_id=run.id,
                agent_name=agent_name,
                celery_task_id=str(uuid4()),
                status="success" if run.status == "completed" else "failure",
                started_at=run.started_at,
                finished_at=run.completed_at,
                retry_count=random.randint(0, 2),
                raw_result={"mock": True, "agent": agent_name},
            )
            db.add(task)
            count += 1
    
    db.commit()
    print(f"   Created {count} agent tasks")


def seed_findings(db: Session, runs: list[models.AnalysisRun]):
    print(f"Seeding findings for completed runs...")
    count = 0
    
    for run in runs:
        if run.status != "completed":
            continue
        
        num_findings = random.randint(5, 30)
        
        for _ in range(num_findings):
            scanner = random.choice(SCANNERS)
            rule_id = random.choice(RULE_IDS[scanner])
            severity = weighted_choice(SEVERITIES, SEVERITY_WEIGHTS)
            
            finding = models.CodeSecFinding(
                run_id=run.id,
                scanner=scanner,
                severity=severity,
                file_path=f"src/{random.choice(['api', 'core', 'utils', 'models', 'services'])}/{random.choice(['auth', 'database', 'handlers', 'validators', 'helpers'])}.py",
                line_number=random.randint(1, 500),
                rule_id=rule_id,
                rule_title=rule_id.replace("-", " ").replace("_", " ").title(),
                description=f"{scanner.title()} detected: {rule_id}",
                remediation_hint=REMEDIATION_HINTS.get(rule_id, "Review and fix the issue"),
                raw_json={
                    "tool": scanner,
                    "rule_id": rule_id,
                    "severity": severity,
                    "file": "example.py",
                    "line": random.randint(1, 100),
                },
                created_at=run.started_at + timedelta(seconds=random.randint(10, 300)),
            )
            db.add(finding)
            count += 1
    
    db.commit()
    print(f"   Created {count} findings")


def seed_cost_estimates(db: Session, runs: list[models.AnalysisRun]):
    print(f"Seeding cost estimates for completed runs...")
    count = 0
    
    for run in runs:
        if run.status != "completed":
            continue
        
        architecture = random.choice(ARCHITECTURES)
        
        if architecture == "ecs_fargate":
            breakdown = [
                {"service": "ECS Fargate", "monthly_cost_usd": round(random.uniform(50, 200), 2)},
                {"service": "ALB", "monthly_cost_usd": round(random.uniform(15, 40), 2)},
                {"service": "RDS PostgreSQL", "monthly_cost_usd": round(random.uniform(20, 100), 2)},
                {"service": "CloudWatch Logs", "monthly_cost_usd": round(random.uniform(5, 20), 2)},
            ]
        elif architecture == "lambda":
            breakdown = [
                {"service": "Lambda", "monthly_cost_usd": round(random.uniform(5, 50), 2)},
                {"service": "API Gateway", "monthly_cost_usd": round(random.uniform(3, 20), 2)},
                {"service": "DynamoDB", "monthly_cost_usd": round(random.uniform(2, 30), 2)},
                {"service": "CloudWatch Logs", "monthly_cost_usd": round(random.uniform(2, 10), 2)},
            ]
        elif architecture == "ec2":
            breakdown = [
                {"service": "EC2 Instances", "monthly_cost_usd": round(random.uniform(80, 300), 2)},
                {"service": "ALB", "monthly_cost_usd": round(random.uniform(15, 40), 2)},
                {"service": "RDS PostgreSQL", "monthly_cost_usd": round(random.uniform(20, 100), 2)},
                {"service": "EBS Volumes", "monthly_cost_usd": round(random.uniform(10, 50), 2)},
            ]
        else:  # hybrid
            breakdown = [
                {"service": "ECS Fargate", "monthly_cost_usd": round(random.uniform(30, 100), 2)},
                {"service": "Lambda", "monthly_cost_usd": round(random.uniform(5, 30), 2)},
                {"service": "ALB", "monthly_cost_usd": round(random.uniform(15, 40), 2)},
                {"service": "RDS PostgreSQL", "monthly_cost_usd": round(random.uniform(20, 100), 2)},
            ]
        
        total_monthly = sum(item["monthly_cost_usd"] for item in breakdown)
        
        for item in breakdown:
            estimate = models.InfracostEstimate(
                run_id=run.id,
                resource_type=item["service"],
                resource_name=item["service"].lower().replace(" ", "-"),
                monthly_cost_usd=item["monthly_cost_usd"],
                annual_cost_usd=round(item["monthly_cost_usd"] * 12, 2),
                usage_assumptions=[
                    {"users": 1000, "estimated_monthly_cost_usd": round(total_monthly * 0.5, 2)},
                    {"users": 10000, "estimated_monthly_cost_usd": round(total_monthly * 1.5, 2)},
                    {"users": 100000, "estimated_monthly_cost_usd": round(total_monthly * 5, 2)},
                ],
                cost_drivers=[
                    {"strategy": "graviton", "projected_savings_usd": round(total_monthly * 0.2, 2)},
                    {"strategy": "reserved_instances", "projected_savings_usd": round(total_monthly * 0.3, 2)},
                ],
                confidence_level=random.choice(["high", "medium", "low"]),
                created_at=run.started_at + timedelta(seconds=random.randint(100, 500)),
            )
            db.add(estimate)
            count += 1
    
    db.commit()
    print(f"   Created {count} cost estimates")


def seed_terraform_artifacts(db: Session, runs: list[models.AnalysisRun]):
    print(f"Seeding Terraform artifacts for completed runs...")
    count = 0
    
    for run in runs:
        if run.status != "completed":
            continue
        
        artifacts = [
            {
                "file_path": "main.tf",
                "content": f'''resource "aws_ecs_cluster" "app" {{
  name = "{run.project.repo_name}-cluster"
}}

resource "aws_ecs_service" "app" {{
  name            = "{run.project.repo_name}-service"
  cluster         = aws_ecs_cluster.app.id
  task_definition = aws_ecs_task_definition.app.arn
  desired_count   = 2
}}''',
            },
            {
                "file_path": "variables.tf",
                "content": f'''variable "aws_region" {{
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}}

variable "environment" {{
  description = "Environment name"
  type        = string
  default     = "dev"
}}

variable "app_name" {{
  description = "Application name"
  type        = string
  default     = "{run.project.repo_name}"
}}''',
            },
            {
                "file_path": "outputs.tf",
                "content": f'''output "alb_dns" {{
  description = "ALB DNS name"
  value       = aws_lb.app.dns_name
}}

output "ecs_cluster_name" {{
  description = "ECS cluster name"
  value       = aws_ecs_cluster.app.name
}}

output "service_name" {{
  description = "ECS service name"
  value       = aws_ecs_service.app.name
}}''',
            },
        ]
        
        for artifact in artifacts:
            ta = models.TerraformArtifact(
                run_id=run.id,
                artifact_type="terraform",
                file_path=artifact["file_path"],
                content=artifact["content"],
                checksum=str(uuid4())[:32],
                created_at=run.started_at + timedelta(seconds=random.randint(200, 600)),
            )
            db.add(ta)
            count += 1
    
    db.commit()
    print(f"   Created {count} Terraform artifacts")


def seed_deployments(db: Session, runs: list[models.AnalysisRun]):
    print(f"Seeding deployments for completed runs...")
    count = 0
    
    for run in runs:
        if run.status != "completed":
            continue
        
        # Not all completed runs have deployments
        if random.random() > 0.7:
            continue
        
        environment = random.choice(ENVIRONMENTS)
        status = weighted_choice(DEPLOYMENT_STATUSES, DEPLOYMENT_STATUS_WEIGHTS)
        
        applied_at = None
        rollback_reason = None
        if status == "succeeded":
            applied_at = run.completed_at
        elif status == "rolled_back":
            applied_at = run.completed_at - timedelta(minutes=random.randint(1, 5))
            rollback_reason = random.choice([
                "Health check failed: 502 Bad Gateway",
                "Health check timeout after 5 minutes",
                "Error rate exceeded 10% threshold",
                "Deployment validation failed",
            ])
        
        deployment = models.Deployment(
            run_id=run.id,
            environment=environment,
            aws_region="us-east-1",
            terraform_version="1.6.0",
            terraform_state_id=f"{run.project.repo_name}-{environment}-{random.randint(1000, 9999)}",
            status=status,
            applied_at=applied_at,
            rollback_reason=rollback_reason,
            infrastructure_json={
                "architecture": random.choice(ARCHITECTURES),
                "region": "us-east-1",
                "environment": environment,
            },
            cost_total_monthly=round(random.uniform(50, 500), 2),
            created_at=run.started_at + timedelta(seconds=random.randint(500, 1000)),
        )
        db.add(deployment)
        count += 1
    
    db.commit()
    print(f"   Created {count} deployments")


def seed_notifications(db: Session, runs: list[models.AnalysisRun], user: models.User):
    print(f"Seeding notifications...")
    count = 0
    
    for run in runs:
        if run.status == "completed":
            if random.random() > 0.3:
                notif = models.Notification(
                    user_id=user.id,
                    run_id=run.id,
                    type="finding",
                    severity=random.choice(["warning", "critical"]),
                    title=f"Security scan completed for {run.project.repo_name}",
                    body=f"Found {random.randint(1, 15)} vulnerabilities. Review findings in dashboard.",
                    is_read=random.choice([True, False]),
                    created_at=run.completed_at,
                    read_at=run.completed_at + timedelta(minutes=random.randint(1, 60)) if random.choice([True, False]) else None,
                )
                db.add(notif)
                count += 1
            
            deployment = db.query(models.Deployment).filter(models.Deployment.run_id == run.id).first()
            if deployment:
                notif = models.Notification(
                    user_id=user.id,
                    run_id=run.id,
                    type="deployment",
                    severity="info" if deployment.status == "succeeded" else "critical",
                    title=f"Deployment {deployment.status} for {run.project.repo_name}",
                    body=f"Environment: {deployment.environment}. Status: {deployment.status}.",
                    is_read=random.choice([True, False]),
                    created_at=deployment.applied_at or run.completed_at,
                )
                db.add(notif)
                count += 1
        
        elif run.status == "failed":
            notif = models.Notification(
                user_id=user.id,
                run_id=run.id,
                type="security_breach",
                severity="critical",
                title=f"Analysis failed for {run.project.repo_name}",
                body="Pipeline execution failed. Check logs for details.",
                is_read=False,
                created_at=run.completed_at,
            )
            db.add(notif)
            count += 1
    
    db.commit()
    print(f"   Created {count} notifications")


def seed_cost_alerts(db: Session, runs: list[models.AnalysisRun], user: models.User):
    print(f"Seeding cost alerts...")
    count = 0
    
    for run in runs:
        if run.status != "completed":
            continue
        
        deployment = db.query(models.Deployment).filter(models.Deployment.run_id == run.id).first()
        if not deployment:
            continue
        
        if random.random() > 0.4:
            continue
        
        alert_type = random.choice(["budget_exceeded", "cost_spike", "unusual_resource"])
        threshold = round(random.uniform(100, 1000), 2)
        actual = threshold * random.uniform(1.1, 3.0)
        
        alert = models.CostAlert(
            run_id=run.id,
            project_id=run.project_id,
            user_id=user.id,
            alert_type=alert_type,
            threshold_usd=threshold,
            actual_cost_usd=round(actual, 2),
            severity=random.choice(["warning", "critical"]),
            is_resolved=random.choice([True, False]),
            created_at=deployment.applied_at + timedelta(days=random.randint(1, 7)) if deployment.applied_at else run.completed_at,
            resolved_at=deployment.applied_at + timedelta(days=random.randint(8, 14)) if random.choice([True, False]) else None,
        )
        db.add(alert)
        count += 1
    
    db.commit()
    print(f"   Created {count} cost alerts")


def print_summary(db: Session):
    print("\n" + "=" * 50)
    print("SEEDING SUMMARY")
    print("=" * 50)
    
    counts = {
        "Users": db.query(models.User).count(),
        "Projects": db.query(models.Project).count(),
        "Runs": db.query(models.AnalysisRun).count(),
        "Agent Tasks": db.query(models.AgentTask).count(),
        "Findings": db.query(models.CodeSecFinding).count(),
        "Cost Estimates": db.query(models.InfracostEstimate).count(),
        "Terraform Artifacts": db.query(models.TerraformArtifact).count(),
        "Deployments": db.query(models.Deployment).count(),
        "Notifications": db.query(models.Notification).count(),
        "Cost Alerts": db.query(models.CostAlert).count(),
    }
    
    for name, count in counts.items():
        print(f"   {name:20s}: {count}")
    
    print("\n   Run Statuses:")
    for status in ["queued", "running", "completed", "failed", "rejected"]:
        count = db.query(models.AnalysisRun).filter(models.AnalysisRun.status == status).count()
        if count:
            print(f"      {status:12s}: {count}")
    
    print("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="Seed DevGuard AI database")
    parser.add_argument("--projects", type=int, default=3, help="Number of projects to seed")
    parser.add_argument("--runs-per-project", type=int, default=5, help="Runs per project")
    parser.add_argument("--clear", action="store_true", help="Clear existing data first")
    parser.add_argument("--only-findings", action="store_true", help="Only seed findings for existing runs")
    parser.add_argument("--only-costs", action="store_true", help="Only seed cost estimates")
    parser.add_argument("--only-deployments", action="store_true", help="Only seed deployments")
    args = parser.parse_args()
    
    print("DevGuard AI Database Seeder")
    print("=" * 50)
    
    init_db()
    db = SessionLocal()
    
    try:
        user = get_or_create_system_user(db)
        print(f"System user: {user.email} (ID: {user.id})")
        
        if args.clear:
            clear_all_data(db)
        
        if args.only_findings:
            runs = db.query(models.AnalysisRun).filter(models.AnalysisRun.status == "completed").all()
            seed_findings(db, runs)
        elif args.only_costs:
            runs = db.query(models.AnalysisRun).filter(models.AnalysisRun.status == "completed").all()
            seed_cost_estimates(db, runs)
            seed_terraform_artifacts(db, runs)
        elif args.only_deployments:
            runs = db.query(models.AnalysisRun).filter(models.AnalysisRun.status == "completed").all()
            seed_deployments(db, runs)
            seed_notifications(db, runs, user)
            seed_cost_alerts(db, runs, user)
        else:
            projects = seed_projects(db, user, args.projects)
            runs = seed_runs(db, projects, user, args.runs_per_project)
            seed_agent_tasks(db, runs)
            seed_findings(db, runs)
            seed_cost_estimates(db, runs)
            seed_terraform_artifacts(db, runs)
            seed_deployments(db, runs)
            seed_notifications(db, runs, user)
            seed_cost_alerts(db, runs, user)
        
        print_summary(db)
        print(f"\nSeeding complete!")
        
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()