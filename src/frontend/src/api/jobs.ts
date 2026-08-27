import { client } from "./client"
import type {
  ApproveRequest,
  JobCreate,
  JobDetail,
  JobListResponse,
  JobResponse,
} from "../types/jobs"
import type { JobResults, TerraformArtifact } from "../types/results"

export interface RollbackRequest {
  reason?: string
  target_revision?: number | null
}

export interface DeploymentRevision {
  task_definition_arn: string
  family: string
  revision: number | null
  is_current: boolean
}

export interface DeploymentRevisionsResponse {
  job_id: string
  service: string
  versions: DeploymentRevision[]
}

export interface RollbackResponse {
  job_id: string
  status: string
  result: {
    status: string
    message?: string
    task_definition?: string
    error?: string
  }
}

export interface ArtifactEdit {
  file_path: string
  content: string
}

export interface ArtifactsEditResponse {
  edited: Array<{ file_path: string; edited_by: string; edited_at: string }>
  written: number
  terraform_artifacts: TerraformArtifact[]
}

export interface CheckUpdateResponse {
  job_id: string
  has_update: boolean
  latest_sha: string
  current_sha: string | null
}

export interface DestroyRequest {
  confirm_service_name: string
}
export interface DestroyResponse {
  job_id: string
  status: string
  result: {
    status: string
    message?: string
    error?: string
    remaining_resources?: {
      ecs_service: { cluster: string; service_name: string; status?: string; running_count?: number } | null
      target_groups: string[]
      error: string | null
    }
  }
}
export interface MonitoringSnapshot {
  checked_at: string
  status: string | null
  desired_count: number | null
  running_count: number | null
  pending_count: number | null
  healthy_targets: number | null
  unhealthy_targets: number | null
  estimated_monthly_cost_usd: number | null
}
export interface MonitoringHistoryResponse {
  job_id: string
  snapshots: MonitoringSnapshot[]
}
export interface MonitoringTargetHealth {
  target_id?: string
  port?: number
  state?: string
  reason?: string
}
export interface MonitoringResponse {
  job_id: string
  ecs_cluster?: string
  service_name?: string
  status: string
  detail?: string
  desired_count?: number
  running_count?: number
  pending_count?: number
  deployments?: Array<{
    status?: string
    rollout_state?: string
    rollout_state_reason?: string
    desired_count?: number
    running_count?: number
  }>
  target_health?: MonitoringTargetHealth[]
  target_health_error?: string | null
  estimated_monthly_cost_usd?: number | null
}
export const jobsApi = {
  create: (body: JobCreate) => client.post<JobResponse>("/jobs/", body),
  remove: (jobId: string) => client.del<{ job_id: string; deleted: boolean }>(`/jobs/${jobId}`),
upload: (formData: FormData) => client.upload<JobResponse>("/jobs/upload", formData),
  list: () => client.get<JobListResponse>("/jobs/"),
  get: (jobId: string) => client.get<JobDetail>(`/jobs/${jobId}`),
  approve: (jobId: string, body: ApproveRequest) => client.post<JobResponse>(`/jobs/${jobId}/approve`, body),
  results: (jobId: string) => client.get<JobResults>(`/jobs/${jobId}/results`),
  editArtifacts: (jobId: string, files: ArtifactEdit[]) =>
    client.put<ArtifactsEditResponse>(`/jobs/${jobId}/artifacts`, { files }),
  rollback: (jobId: string, body: RollbackRequest) =>
    client.post<RollbackResponse>(`/jobs/${jobId}/rollback`, body),
  destroy: (jobId: string, body: DestroyRequest) =>
    client.post<DestroyResponse>(`/jobs/${jobId}/destroy`, body),
  deploymentRevisions: (jobId: string) =>
    client.get<DeploymentRevisionsResponse>(`/jobs/${jobId}/deployments/revisions`),
  monitoring: (jobId: string) =>
    client.get<MonitoringResponse>(`/jobs/${jobId}/monitoring`),
  monitoringHistory: (jobId: string) =>
    client.get<MonitoringHistoryResponse>(`/jobs/${jobId}/monitoring/history`),
  checkUpdate: (jobId: string) =>
    client.get<CheckUpdateResponse>(`/jobs/${jobId}/check-update`),
  triggerUpdate: (jobId: string) =>
    client.post<JobResponse>(`/jobs/${jobId}/update`),
}
