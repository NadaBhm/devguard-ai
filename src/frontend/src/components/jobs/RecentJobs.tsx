import { useQuery, useQueryClient } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { jobsApi } from "../../api/jobs"
import { RunStatusBadge } from "./RunStatusBadge"
import { formatDate, relativeTime } from "../../lib/format"

// Backend refuses deleting runs whose deployment still tracks live infra.
const DELETABLE = new Set(["destroyed", "failed"])

function DeleteJobButton({ jobId }: { jobId: string }) {
  const queryClient = useQueryClient()
  const onClick = async (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (!window.confirm("Delete this job and its entire history? This cannot be undone.")) return
    try {
      await jobsApi.remove(jobId)
      await queryClient.invalidateQueries({ queryKey: ["jobs"] })
    } catch (err) {
      const detail =
        err instanceof Error && err.message ? err.message : "Delete failed"
      window.alert(detail)
    }
  }
  return (
    <button
      onClick={onClick}
      title="Delete job history"
      aria-label={`Delete job ${jobId}`}
      className="rounded p-1 text-faint opacity-0 transition-opacity hover:bg-surface-2 hover:text-danger focus:opacity-100 group-hover:opacity-100"
    >
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2">
        <path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2m3 0v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" />
      </svg>
    </button>
  )
}

export function RecentRuns({ limit = 6, className }: { limit?: number; className?: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["jobs"],
    queryFn: jobsApi.list,
  })

  if (isLoading) {
    return (
      <div className={`space-y-2 ${className ?? ""}`}>
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="h-11 animate-pulse rounded-md bg-surface-2" />
        ))}
      </div>
    )
  }

  if (isError || !data) {
    return <p className="text-[13px] text-faint">Unable to load recent runs.</p>
  }

  const jobs = data.jobs.slice(0, limit)

  if (jobs.length === 0) {
    return <p className="text-[13px] text-faint">No runs yet Start your first analysis.</p>
  }

  return (
    <div className={className ?? ""}>
      {jobs.map((job) => (
        <Link
          key={job.job_id}
          to={`/runs/${job.job_id}`}
          className="group flex items-center gap-3 border-b border-border px-1 py-2.5 last:border-0 transition-colors hover:bg-surface-2/60"
        >
          <div className="min-w-0 flex-1">
            <p className="truncate font-mono text-[12.5px] text-foreground group-hover:text-accent">
              {job.repo_url ? job.repo_url.replace(/^https?:\/\/(www\.)?/, "") : "unknown repo"}
            </p>
            <p className="mt-0.5 text-[11.5px] text-faint" title={formatDate(job.started_at)}>
              {relativeTime(job.started_at)}
              {job.duration_seconds ? ` · ${job.duration_seconds}s` : ""}
            </p>
          </div>
          <RunStatusBadge status={job.status} />
          {DELETABLE.has(job.status) && <DeleteJobButton jobId={job.job_id} />}
        </Link>
      ))}
    </div>
  )
}