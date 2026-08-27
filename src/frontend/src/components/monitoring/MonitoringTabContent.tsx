import { useQuery } from "@tanstack/react-query"
import { jobsApi } from "../../api/jobs"
import type { MonitoringSnapshot } from "../../api/jobs"
import type { Deployment } from "../../types/results"
import { formatCurrency, formatDate } from "../../lib/format"
import { Badge } from "../ui/Badge"
import { Spinner } from "../ui/Button"

const POLL_MS = 15000

function statusTone(status: string | undefined): "accent" | "neutral" | "success" | "warning" | "danger" {
  if (status === "ACTIVE") return "success"
  if (status === "not_found") return "neutral"
  if (status === "DRAINING") return "warning"
  return "neutral"
}

function buildLinePath(
  values: Array<number | null>,
  width: number,
  height: number,
  padding: number,
): { path: string; points: Array<{ x: number; y: number; value: number }> } {
  const usable = values
    .map((v, i) => ({ v, i }))
    .filter((entry): entry is { v: number; i: number } => entry.v != null)

  if (usable.length === 0) return { path: "", points: [] }

  const min = Math.min(...usable.map((e) => e.v))
  const max = Math.max(...usable.map((e) => e.v))
  const span = max - min || 1
  const stepX = values.length > 1 ? (width - padding * 2) / (values.length - 1) : 0

  const points = usable.map((e) => {
    const x = padding + e.i * stepX
    const y = padding + (1 - (e.v - min) / span) * (height - padding * 2)
    return { x, y, value: e.v }
  })

  const path = points.map((p, idx) => `${idx === 0 ? "M" : "L"}${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" ")
  return { path, points }
}

function MonitoringChart({ snapshots }: { snapshots: MonitoringSnapshot[] }) {
  const width = 560
  const height = 170
  const padding = 24

  const runningValues = snapshots.map((s) => (s.running_count != null ? s.running_count : null))
  const costValues = snapshots.map((s) =>
    s.estimated_monthly_cost_usd != null ? s.estimated_monthly_cost_usd : null,
  )

  const running = buildLinePath(runningValues, width, height, padding)
  const cost = buildLinePath(costValues, width, height, padding)

  const hasAnyData = running.points.length > 0 || cost.points.length > 0

  if (!hasAnyData) {
    return (
      <div className="flex items-center justify-center py-10 text-[12.5px] text-faint">
        Not enough history yet -- keep this tab open and it will fill in.
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full" role="img" aria-label="Running tasks and estimated cost over time">
        <line x1={padding} y1={height - padding} x2={width - padding} y2={height - padding} stroke="currentColor" className="text-border" strokeWidth="1" />
        {running.path && <path d={running.path} fill="none" stroke="currentColor" className="text-accent" strokeWidth="2" />}
        {cost.path && <path d={cost.path} fill="none" stroke="currentColor" className="text-warning" strokeWidth="2" strokeDasharray="4 3" />}
        {running.points.map((p, i) => (
          <circle key={`r-${i}`} cx={p.x} cy={p.y} r="2.5" fill="currentColor" className="text-accent" />
        ))}
        {cost.points.map((p, i) => (
          <circle key={`c-${i}`} cx={p.x} cy={p.y} r="2.5" fill="currentColor" className="text-warning" />
        ))}
      </svg>
      <div className="flex items-center gap-4 text-[11.5px] text-muted">
        <span className="flex items-center gap-1.5">
          <span className="size-2 rounded-full bg-accent" />
          Running tasks
        </span>
        <span className="flex items-center gap-1.5">
          <span className="size-2 rounded-full bg-warning" />
          Estimated cost / mo
        </span>
      </div>
    </div>
  )
}

export function MonitoringTab({ jobId, deployments }: { jobId?: string; deployments: Deployment[] }) {
  const canMonitor = !!jobId && deployments.some((d) => d.status === "succeeded" || d.status === "destroyed")

  const monitoringQuery = useQuery({
    queryKey: ["monitoring", jobId],
    queryFn: () => jobsApi.monitoring(jobId!),
    enabled: canMonitor,
    refetchInterval: POLL_MS,
    retry: false,
  })

  const historyQuery = useQuery({
    queryKey: ["monitoring-history", jobId],
    queryFn: () => jobsApi.monitoringHistory(jobId!),
    enabled: canMonitor,
    refetchInterval: POLL_MS,
  })

  if (!canMonitor) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <p className="max-w-sm text-[13px] text-muted">
          No live deployment to monitor for this job yet.
        </p>
      </div>
    )
  }

  const data = monitoringQuery.data
  const snapshots = historyQuery.data?.snapshots ?? []

  return (
    <div className="space-y-3 p-4">
      <div className="rounded-lg border border-border bg-surface px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-[13px] font-medium text-foreground">Live status</p>
            <p className="text-[12px] text-muted">
              Refreshes automatically every {POLL_MS / 1000}s -- queries AWS directly, not just what Terraform last reported.
            </p>
          </div>
          {monitoringQuery.isFetching && <Spinner size={14} />}
        </div>

        {monitoringQuery.isLoading && (
          <div className="flex items-center gap-2 py-6 text-[12.5px] text-faint">
            <Spinner size={16} />
            Loading live status…
          </div>
        )}

        {monitoringQuery.isError && (
          <p className="mt-3 text-[12.5px] text-critical">
            {monitoringQuery.error instanceof Error ? monitoringQuery.error.message : "Could not fetch live status."}
          </p>
        )}

        {data && (
          <div className="mt-3 space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={statusTone(data.status)}>{data.status}</Badge>
              {data.ecs_cluster && <span className="font-mono text-[11.5px] text-faint">{data.ecs_cluster}</span>}
              {data.service_name && <span className="font-mono text-[11.5px] text-faint">/ {data.service_name}</span>}
            </div>

            {data.detail && <p className="text-[12.5px] text-muted">{data.detail}</p>}

            {data.status !== "not_found" && (
              <dl className="grid grid-cols-3 gap-3 text-[12.5px]">
                <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
                  <dt className="text-faint">Running / Desired</dt>
                  <dd className="mt-0.5 font-medium tabular text-foreground">
                    {data.running_count ?? "—"} / {data.desired_count ?? "—"}
                  </dd>
                </div>
                <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
                  <dt className="text-faint">Pending</dt>
                  <dd className="mt-0.5 font-medium tabular text-foreground">{data.pending_count ?? "—"}</dd>
                </div>
                <div className="rounded-md border border-border bg-surface-2 px-3 py-2">
                  <dt className="text-faint">Cost / mo</dt>
                  <dd className="mt-0.5 font-medium tabular text-foreground">
                    {data.estimated_monthly_cost_usd != null ? formatCurrency(data.estimated_monthly_cost_usd) : "—"}
                  </dd>
                </div>
              </dl>
            )}

            {data.target_health && data.target_health.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-[12px] font-medium text-foreground">Target health</p>
                <ul className="space-y-1">
                  {data.target_health.map((t, i) => (
                    <li key={i} className="flex items-center gap-2 text-[12px]">
                      <span
                        className={`size-1.5 rounded-full ${t.state === "healthy" ? "bg-accent" : "bg-critical"}`}
                      />
                      <span className="font-mono text-muted">{t.target_id ?? "unknown"}</span>
                      <span className="text-faint">{t.state ?? "unknown"}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {data.target_health_error && (
              <p className="text-[11.5px] text-faint">Target group check: {data.target_health_error}</p>
            )}
          </div>
        )}
      </div>

      <div className="rounded-lg border border-border bg-surface px-4 py-3">
        <p className="text-[13px] font-medium text-foreground">History</p>
        <p className="mb-2 text-[12px] text-muted">
          Built from every check above -- running task count (solid) and estimated cost (dashed) over time.
        </p>
        <MonitoringChart snapshots={snapshots} />
        {snapshots.length > 0 && (
          <p className="mt-2 text-[11px] text-faint">
            Latest check: {formatDate(snapshots[snapshots.length - 1].checked_at)}
          </p>
        )}
      </div>
    </div>
  )
}