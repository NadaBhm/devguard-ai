import { client } from "./client"
import { tokenStore } from "./tokenStore"
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
export const jobsApi = {
  create: (body: JobCreate) => client.post<JobResponse>("/jobs/", body),
  upload: (formData: FormData) => fetch(`/api/jobs/upload`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${tokenStore.access ?? ""}`,
    },
    body: formData,
  }).then(async (res) => {
    if (!res.ok) {
      let detail = res.statusText
      try {
        const body = (await res.json()) as { detail?: string }
        if (typeof body.detail === "string") detail = body.detail
      } catch {
        // non-JSON body is fine
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
  destroy: (jobId: string, body: DestroyRequest) =>
    client.post<DestroyResponse>(`/jobs/${jobId}/destroy`, body),
  deploymentRevisions: (jobId: string) =>
    client.get<DeploymentRevisionsResponse>(`/jobs/${jobId}/deployments/revisions`),
  checkUpdate: (jobId: string) =>
    client.get<CheckUpdateResponse>(`/jobs/${jobId}/check-update`),
  triggerUpdate: (jobId: string) =>
    client.post<JobResponse>(`/jobs/${jobId}/update`),
}
