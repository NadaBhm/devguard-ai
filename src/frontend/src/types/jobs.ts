export type RunState =
  | "pending"
  | "cloning"
  | "analyzing"
  | "awaiting_approval_gate_1"
  | "infra_generating"
  | "awaiting_approval_gate_2"
  | "deploying"
  | "health_checking"
  | "completed"
  | "failed"
  | "rolled_back"
  | "rejected"

export type RunStatus = "queued" | "running" | "completed" | "failed"
export type GateName = "gate_1_pre_infracost" | "gate_2_pre_deployops"

export interface JobSummary {
  job_id: string
  status: RunStatus
  repo_url: string | null
  started_at: string
  completed_at: string | null
  duration_seconds: number | null
}

export interface JobListResponse {
  jobs: JobSummary[]
}

export type SourceType = "github" | "gitlab" | "local_folder"

export interface JobCreate {
  repo_url?: string
  source_type?: SourceType
  commit_sha?: string
  commit_message?: string
  default_branch?: string
}

export interface HumanGate {
  required: boolean
  approved: boolean | null
  comment: string | null
  approved_at: string | null
  approved_by: string | null
  requested_changes?: string | null
}

export interface HumanGates {
  gate_1_pre_infracost: HumanGate
  gate_2_pre_deployops: HumanGate
}

export interface ErrorEntry {
  node: string
  attempt: number
  max_attempts: number
  message: string
  timestamp: string
  stack_trace: string | null
  resolved: boolean
}

export interface OrchestratorMetadata {
  graph_version: string
  start_time: string
  elapsed_seconds: number
  current_node: string
  nodes_executed: string[]
  chat_session_id: string | null
}

export interface GateContext {
  security_score?: number
  grade?: string
  vulnerabilities_count?: number
  secrets_count?: number
  recommendations?: string[]
  monthly_cost_usd?: number
  architecture?: string
  breakdown?: CostItem[]
  iteration?: number
  max_iterations?: number
}

export interface InfracostIteration {
  iteration: number
  prompt: string
  result: InfraCostResult
  requested_at: string
}

export interface Gate {
  gate: string
  message: string
  context?: GateContext
  actions: string[]
}

export interface JobState {
  job_id?: string
  repo_url?: string
  status?: RunState
  created_at?: string
  updated_at?: string
  codesec_result?: CodeSecResult | null
  infracost_result?: InfraCostResult | null
  deployops_result?: DeployOpsResult | null
  human_gates?: HumanGates
  error_log?: ErrorEntry[]
  orchestrator_metadata?: OrchestratorMetadata
  final_report?: FinalReport | null
  infracost_feedback?: string | null
  infracost_iterations?: InfracostIteration[]
  __interrupt__?: unknown[]
}

export interface JobResponse {
  job_id?: string
  status: RunStatus
  orchestrator_status?: string
  gate: string | null
  mode?: "real" | "mixed" | "mock"
  state?: JobState
}

export interface JobDetail extends JobSummary {
  orchestrator_status: string | null
  gate: string | null
  mode?: "real" | "mixed" | "mock"
  state?: JobState
}

export interface ApproveRequest {
  approved: boolean
  comment?: string
  approved_by?: string
  request_regeneration?: boolean
}

export interface CodeSecResult {
  [key: string]: unknown
}

export interface CostItem {
  resource_type: string
  resource_name: string
  monthly_cost_usd: number
  [key: string]: unknown
}

export interface CostEstimate {
  monthly_cost_usd?: number
  annual_cost_usd?: number
  breakdown?: CostItem[]
  [key: string]: unknown
}

export interface InfraCostResult {
  architecture_recommendation?: "ecs_fargate" | "lambda" | "ec2" | "hybrid"
  justification?: string
  generated_terraform?: Record<string, string>
  cost_estimate?: CostEstimate
  load_scenarios?: unknown[]
  optimizations?: unknown[]
  region_comparison?: unknown[]
  [key: string]: unknown
}

export interface HealthCheckResult {
  passed: boolean
  response_time_ms: number
  status_code: number
  checked_at: string | null
}

export interface DeployOpsResult {
  job_id?: string
  deployment_status?: "success" | "failed" | "rolled_back"
  deployed_url?: string | null
  health_check?: HealthCheckResult
  rollback_triggered?: boolean
  rollback_reason?: string | null
  error?: string | null
  terraform_outputs?: { [key: string]: string }
  artifacts?: unknown
  aws_config?: unknown
  deployment_config?: unknown
  approval?: unknown
  [key: string]: unknown
}

export interface FinalReport {
  format: "pdf" | "html" | "json"
  generated_at: string
  download_url?: string | null
  summary: {
    total_vulnerabilities: number
    critical_count: number
    estimated_monthly_cost_usd: number
    deployment_status: string
    recommendations: string[]
    pipeline_duration_seconds: number
  }
}