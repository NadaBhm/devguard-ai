import { client } from "./client"
import type {
  ApproveRequest,
  JobCreate,
  JobDetail,
  JobListResponse,
  JobResponse,
} from "../types/jobs"
import type { JobResults } from "../types/results"

export const jobsApi = {
  create: (body: JobCreate) => client.post<JobResponse>("/jobs/", body),
  list: () => client.get<JobListResponse>("/jobs"),
  get: (jobId: string) => client.get<JobDetail>(`/jobs/${jobId}`),
  approve: (jobId: string, body: ApproveRequest) => client.post<JobResponse>(`/jobs/${jobId}/approve`, body),
  results: (jobId: string) => client.get<JobResults>(`/jobs/${jobId}/results`),
}