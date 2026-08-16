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
export interface MonitoringDeployment {
  status: string | null
  rollout_state: string | null
  rollout_state_reason: string | null
  desired_count: number | null
  running_count: number | null
}
export interface MonitoringTargetHealth {
  target_id: string | null
  port: number | null
  state: string | null
  reason: string | null
}
export interface MonitoringResponse {
  job_id: string
  ecs_cluster: string
  service_name: string
  status: string
  desired_count?: number | null
  running_count?: number | null
  pending_count?: number | null
  deployments?: MonitoringDeployment[]
  target_health?: MonitoringTargetHealth[]
  target_health_error?: string | null
  estimated_monthly_cost_usd?: number | null
  detail?: string
}

export const jobsApi = {
  create: (body: JobCreate) => client.post<JobResponse>("/jobs/", body),
  upload: (formData: FormData) => fetch(`/api/jobs/upload`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${localStorage.getItem("access_token") ?? ""}`,
    },
    body: formData,
  }).then(async (res) => {
    if (!res.ok) {
      let detail = res.statusText
      try {
        const body = (await res.json()) as { detail?: string }
        if (typeof body.detail === "string") detail = body.detail
      } catch {
        // ignore invalid JSON responses
      }
      throw new Error(detail)
    }
    return res.json() as Promise<JobResponse>
  }),
  list: () => client.get<JobListResponse>("/jobs/"),
  get: (jobId: string) => client.get<JobDetail>(`/jobs/${jobId}`),
  approve: (jobId: string, body: ApproveRequest) => client.post<JobResponse>(`/jobs/${jobId}/approve`, body),
  results: (jobId: string) => client.get<JobResults>(`/jobs/${jobId}/results`),
  editArtifacts: (jobId: string, files: ArtifactEdit[]) =>
    client.put<ArtifactsEditResponse>(`/jobs/${jobId}/artifacts`, { files }),
  rollback: (jobId: string, body: RollbackRequest) =>
    client.post<RollbackResponse>(`/jobs/${jobId}/rollback`, body),
  deploymentRevisions: (jobId: string) =>
    client.get<DeploymentRevisionsResponse>(`/jobs/${jobId}/deployments/revisions`),
  monitoring: (jobId: string) =>
    client.get<MonitoringResponse>(`/jobs/${jobId}/monitoring`),
}
