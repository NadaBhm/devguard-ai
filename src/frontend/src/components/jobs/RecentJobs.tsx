import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { jobsApi } from "../../api/jobs"
import { RunStatusBadge } from "./RunStatusBadge"
import { formatDate, relativeTime } from "../../lib/format"

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
        </Link>
      ))}
    </div>
  )
}