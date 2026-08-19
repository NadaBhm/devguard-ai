export type Severity = "critical" | "high" | "medium" | "low"
export type Scanner = "semgrep" | "gitleaks" | "trivy" | "bandit"
export type AgentTaskStatus = "pending" | "started" | "success" | "failure" | "retrying"
export type DeploymentStatus = "pending" | "applying" | "succeeded" | "failed" | "rolled_back" | "destroyed"
export type Environment = "dev" | "staging" | "prod"

export interface CodeSecFinding {
  id: string
  run_id: string
  scanner: Scanner
  severity: Severity
  file_path: string
  line_number: number | null
  rule_id: string
  rule_title: string
  description: string
  remediation_hint: string | null
  created_at: string
}

export interface InfracostEstimate {
  id: string
  run_id: string
  resource_type: string
  resource_name: string
  monthly_cost_usd: string | number
  annual_cost_usd: string | number
  usage_assumptions: Record<string, unknown> | null
  cost_drivers: Record<string, unknown> | null
  confidence_level: "high" | "medium" | "low" | null
  created_at: string
}

export interface TerraformArtifact {
  id: string
  run_id: string
  artifact_type: string
  file_path: string
  content: string
  checksum: string | null
  edited_by: string | null
  edited_at: string | null
  created_at: string
}

export interface Deployment {
  id: string
  run_id: string
  environment: Environment
  aws_region: string
  terraform_version: string | null
  terraform_state_id: string | null
  status: DeploymentStatus
  applied_at: string | null
  rollback_reason: string | null
  infrastructure_json: Record<string, unknown> | null
  cost_total_monthly: string | number | null
  created_at: string
}

export interface AgentTask {
  id: string
  run_id: string
  agent_name: "codesec" | "infracost" | "deployops"
  celery_task_id: string
  status: AgentTaskStatus
  started_at: string | null
  finished_at: string | null
  error_message: string | null
  retry_count: number
  raw_result: Record<string, unknown> | null
}

export interface JobResults {
  job_id: string
  status: string
  agent_tasks: AgentTask[]
  codesec_findings: CodeSecFinding[]
  infracost_estimates: InfracostEstimate[]
  terraform_artifacts: TerraformArtifact[]
  deployments: Deployment[]
}