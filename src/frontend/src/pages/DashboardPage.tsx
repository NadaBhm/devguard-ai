import { useMemo } from "react"
import { Link } from "react-router-dom"
import { useQuery } from "@tanstack/react-query"
import { jobsApi } from "../api/jobs"
import { projectsApi } from "../api/projects"
import { Card, Surface } from "../components/ui/Card"
import { Button } from "../components/ui/Button"
import { EmptyState } from "../components/ui/Misc"
import { PageHeader } from "../components/ui/Misc"
import { RunStatusBadge } from "../components/jobs/RunStatusBadge"
import { IconPlus, IconRepo } from "../components/icons"
import { formatCurrency, formatDate, formatDuration } from "../lib/format"
import type { JobSummary, RunStatus } from "../types/jobs"

function useJobsWithResults(jobs: JobSummary[] | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ["dashboard-results", jobs?.map((j) => j.job_id) ?? []],
    queryFn: async () => {
      const rows = await Promise.all(
        jobs!.map(async (job) => {
          try {
            const r = await jobsApi.results(job.job_id)
            return {
              job,
              findings: r.codesec_findings.length,
              costMonthly: r.infracost_estimates.reduce((s, e) => s + Number(e.monthly_cost_usd || 0), 0),
            }
          } catch {
            return { job, findings: 0, costMonthly: 0 }
          }
        }),
      )
      return rows
    },
    enabled,
  })
}

function StatCard({
  label,
  value,
  icon,
  tone = "neutral",
}: {
  label: string
  value: string
  icon?: React.ReactNode
  tone?: "neutral" | "success" | "warning" | "danger"
}) {
  const tones: Record<string, string> = {
    neutral: "text-foreground",
    success: "text-accent",
    warning: "text-medium",
    danger: "text-critical",
  }
  return (
    <div className="rounded-lg border border-border bg-surface px-4 py-3.5">
      <div className="flex items-center justify-between">
        <p className="text-[11px] font-medium uppercase tracking-wider text-faint">{label}</p>
        {icon && <span className="text-faint">{icon}</span>}
      </div>
      <p className={`mt-1.5 text-2xl font-semibold tabular tracking-tight ${tones[tone]}`}>{value}</p>
    </div>
  )
}

function ProgressBar({
  label,
  value,
  total,
  tone,
}: {
  label: string
  value: number
  total: number
  tone: string
}) {
  const pct = total > 0 ? Math.round((value / total) * 100) : 0
  return (
    <div>
      <div className="flex items-center justify-between text-[12px]">
        <span className="text-muted">{label}</span>
        <span className="tabular text-faint">
          {value} · {pct}%
        </span>
      </div>
      <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2">
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

type DashboardRow = { job: JobSummary; findings: number; costMonthly: number }
const EMPTY_ROWS: DashboardRow[] = []

export function DashboardPage() {
  const jobsQuery = useQuery({ queryKey: ["jobs"], queryFn: jobsApi.list })
  const projectsQuery = useQuery({ queryKey: ["projects"], queryFn: projectsApi.list })
  const jobs = jobsQuery.data?.jobs

  const detailed = useJobsWithResults(jobs, !!jobs)
  const detailRows = detailed.data ?? EMPTY_ROWS
  const projectsCount = projectsQuery.data?.length ?? 0

  const stats = useMemo(() => {
    const totalRuns = jobs?.length ?? 0
    const byStatus = (jobs ?? []).reduce<Record<RunStatus, number>>(
      (acc, j) => {
        acc[j.status] += 1
        return acc
      },
      { queued: 0, running: 0, completed: 0, failed: 0 },
    )
    const totalFindings = detailRows.reduce((s, r) => s + r.findings, 0)
    const totalMonthly = detailRows.reduce((s, r) => s + r.costMonthly, 0)
    const latest = jobs?.[0]
    return { totalRuns, byStatus, totalFindings, totalMonthly, projectsCount, latest }
  }, [jobs, detailRows, projectsCount])

  return (
    <div className="space-y-6">
      <PageHeader
        title="Dashboard"
        description="Overview of your repositories, security posture, and infrastructure spend."
        actions={
          <Button variant="primary" icon={<IconPlus className="size-4" />} to="/projects/new">
            New run
          </Button>
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="Projects" value={String(stats.projectsCount)} icon={<IconRepo className="size-4" />} />
        <StatCard label="Total runs" value={String(stats.totalRuns)} />
        <StatCard label="Findings" value={String(stats.totalFindings)} tone={stats.totalFindings > 0 ? "warning" : "success"} />
        <StatCard label="Est. spend / mo" value={formatCurrency(stats.totalMonthly)} />
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[1fr_320px]">
        <Card title="Recent runs" description="Latest analysis jobs across your repositories.">
          {jobsQuery.isLoading ? (
            <div className="space-y-2">
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="h-11 animate-pulse rounded-md bg-surface-2" />
              ))}
            </div>
          ) : jobs && jobs.length > 0 ? (
            <table className="w-full text-[13px]">
              <tbody>
                {jobs.slice(0, 8).map((job) => {
                  const row = detailRows.find((r) => r.job.job_id === job.job_id)
                  return (
                    <tr key={job.job_id} className="border-b border-border last:border-0">
                      <td className="py-2.5 pr-3">
                        <Link to={`/runs/${job.job_id}`} className="block truncate font-mono text-[12.5px] text-foreground hover:text-accent">
                          {(job.repo_url ?? "unknown repo").replace(/^https?:\/\//, "")}
                        </Link>
                        <span className="text-[11.5px] text-faint">
                          {formatDuration(job.duration_seconds)} · {formatDate(job.started_at)}
                        </span>
                      </td>
                      <td className="py-2.5 pr-3 text-right tabular text-muted">{formatCurrency(row?.costMonthly)}</td>
                      <td className="py-2.5 pr-3 text-right tabular text-muted">{row?.findings ?? "—"}</td>
                      <td className="py-2.5 text-right">
                        <RunStatusBadge status={job.status} />
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          ) : (
            <div className="py-4">
              <EmptyState
                title="No runs yet"
                description="Run your first analysis to see security findings and cost estimates."
                action={
                  <Button to="/projects/new" variant="primary">
                    New run
                  </Button>
                }
              />
            </div>
          )}
        </Card>

        <div className="space-y-5">
          <Surface className="p-4">
            <p className="text-[11px] font-medium uppercase tracking-wider text-faint">Run status</p>
            <div className="mt-3 space-y-2.5">
              <ProgressBar label="Queued" value={stats.byStatus.queued} total={stats.totalRuns} tone="bg-medium" />
              <ProgressBar label="Running" value={stats.byStatus.running} total={stats.totalRuns} tone="bg-low" />
              <ProgressBar label="Completed" value={stats.byStatus.completed} total={stats.totalRuns} tone="bg-accent" />
              <ProgressBar label="Failed" value={stats.byStatus.failed} total={stats.totalRuns} tone="bg-critical" />
            </div>
          </Surface>

          <Card title="Latest project">
            {stats.latest ? (
              <div className="space-y-3">
                <Link to={`/runs/${stats.latest.job_id}`} className="block truncate font-mono text-[12.5px] text-foreground hover:text-accent">
                  {(stats.latest.repo_url ?? "unknown").replace(/^https?:\/\//, "")}
                </Link>
                <RunStatusBadge status={stats.latest.status} />
                <p className="text-[12px] text-faint">
                  Started {formatDate(stats.latest.started_at)} · {formatDuration(stats.latest.duration_seconds)}
                </p>
              </div>
            ) : (
              <p className="text-[13px] text-faint">Nothing analyzed yet.</p>
            )}
          </Card>
        </div>
      </div>
    </div>
  )
}