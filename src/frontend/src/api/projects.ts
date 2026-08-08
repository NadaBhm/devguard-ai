import { jobsApi } from "./jobs"
import type { JobSummary } from "../types/jobs"

export interface ProjectSummary {
  repo_name: string
  repo_url: string
  last_run: JobSummary | null
  run_count: number
}

function repoName(url: string): string {
  return url.replace(/\/+$/, "").split("/").pop() ?? url
}

export const projectsApi = {
  async list(): Promise<ProjectSummary[]> {
    const { jobs } = await jobsApi.list()
    const byRepo = new Map<string, ProjectSummary>()
    for (const job of jobs) {
      const url = job.repo_url ?? "unknown"
      const existing = byRepo.get(url)
      if (!existing) {
        byRepo.set(url, {
          repo_name: repoName(url),
          repo_url: url,
          last_run: job,
          run_count: 1,
        })
      } else {
        existing.run_count += 1
        if (
          !existing.last_run ||
          new Date(job.started_at) > new Date(existing.last_run.started_at)
        ) {
          existing.last_run = job
        }
      }
    }
    return Array.from(byRepo.values()).sort((a, b) => {
      const aTime = a.last_run?.started_at ?? ""
      const bTime = b.last_run?.started_at ?? ""
      return bTime.localeCompare(aTime)
    })
  },
}