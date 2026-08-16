import { useQuery } from "@tanstack/react-query"
import { jobsApi } from "../../api/jobs"
import { Card } from "../ui/Card"
import { Spinner } from "../ui/Button"

function StateBadge({ state }: { state: string | null }) {
  const color =
    state === "healthy"
      ? "border-good/25 bg-good/10 text-good"
      : state === "unhealthy" || state === "draining"
        ? "border-critical/25 bg-critical/10 text-critical"
        : "border-border bg-surface-2 text-muted"
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[11px] font-medium ${color}`}>
      {state ?? "unknown"}
    </span>
  )
}

export function MonitoringTab({ jobId }: { jobId: string }) {
  const query = useQuery({
    queryKey: ["job-monitoring", jobId],
    queryFn: () => jobsApi.monitoring(jobId),
    // Live AWS status is worth polling more slowly than the job/results
    // queries -- it never changes as fast as the pipeline itself, and each
    // call makes real AWS API requests (describe_services +
    // describe_target_health per target group).
    refetchInterval: 15000,
    retry: false,
  })

  if (query.isLoading) {
    return (
      <div className="flex items-center justify-center gap-3 py-16 text-muted">
        <Spinner size={18} />
        <span className="text-sm">Loading live status…</span>
      </div>
    )
  }

  if (query.isError) {
    const message =
      query.error instanceof Error ? query.error.message : "Could not load live AWS status."
    return (
      <div className="py-16 text-center">
        <p className="text-sm text-faint">{message}</p>
      </div>
    )
  }

  const data = query.data
  if (!data) return null

  return (
    <div className="space-y-5 p-5">
      <Card title="ECS Service">
        <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-[13px] sm:grid-cols-4">
          <div>
            <dt className="text-faint">Cluster</dt>
            <dd className="mt-0.5 font-mono text-foreground">{data.ecs_cluster}</dd>
          </div>
          <div>
            <dt className="text-faint">Service</dt>
            <dd className="mt-0.5 font-mono text-foreground">{data.service_name}</dd>
          </div>
          <div>
            <dt className="text-faint">Status</dt>
            <dd className="mt-0.5 text-foreground">{data.status}</dd>
          </div>
          <div>
            <dt className="text-faint">Tasks</dt>
            <dd className="mt-0.5 tabular text-foreground">
              {data.running_count ?? "–"} / {data.desired_count ?? "–"} running
            </dd>
          </div>
        </dl>
      </Card>

      {data.deployments && data.deployments.length > 0 && (
        <Card title="Rollout">
          <ul className="space-y-2">
            {data.deployments.map((d, i) => (
              <li key={i} className="text-[13px]">
                <span className="font-mono text-foreground">{d.status}</span>{" "}
                <span className="text-faint">— {d.rollout_state}</span>
                {d.rollout_state_reason && (
                  <p className="mt-0.5 text-[12px] text-muted">{d.rollout_state_reason}</p>
                )}
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card title="Target Health (ALB)">
        {data.target_health_error ? (
          <p className="text-[13px] text-muted">{data.target_health_error}</p>
        ) : data.target_health && data.target_health.length > 0 ? (
          <ul className="divide-y divide-border">
            {data.target_health.map((t, i) => (
              <li key={i} className="flex items-center gap-3 py-2 text-[13px]">
                <span className="font-mono text-foreground">{t.target_id}:{t.port}</span>
                <StateBadge state={t.state} />
                {t.reason && <span className="text-faint">{t.reason}</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[13px] text-faint">No targets registered.</p>
        )}
      </Card>

      {data.estimated_monthly_cost_usd != null && (
        <Card title="Estimated Cost">
          <p className="text-lg font-semibold tabular text-foreground">
            ${data.estimated_monthly_cost_usd.toFixed(2)}
            <span className="ml-1 text-[13px] font-normal text-faint">/ month (InfraCost estimate)</span>
          </p>
        </Card>
      )}
    </div>
  )
}
