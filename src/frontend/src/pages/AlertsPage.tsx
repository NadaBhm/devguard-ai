import { useQuery } from "@tanstack/react-query"
import { alertsApi } from "../api/alerts"
import { Card } from "../components/ui/Card"
import { Badge } from "../components/ui/Badge"
import { EmptyState } from "../components/ui/Misc"
import { PageHeader } from "../components/ui/Misc"
import { IconAlert } from "../components/icons"
import { formatCurrency, formatDate } from "../lib/format"
import type { CostAlert, CostAlertType } from "../types/alert"

const typeMeta: Record<CostAlertType, string> = {
  budget_exceeded: "Budget exceeded",
  cost_spike: "Cost spike",
  unusual_resource: "Unusual resource",
}

function Row({ alert }: { alert: CostAlert }) {
  const threshold = Number(alert.threshold_usd || 0)
  const actual = Number(alert.actual_cost_usd || 0)
  const over = actual > threshold && !alert.is_resolved

  return (
    <li className="border-b border-border px-4 py-3 last:border-0">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-start gap-3">
          <span className="mt-0.5 flex size-7 items-center justify-center rounded-md border border-border bg-surface-2 text-muted">
            <IconAlert className="size-4" />
          </span>
          <div>
            <div className="flex items-center gap-2">
              <p className="text-[13px] font-medium text-foreground">{typeMeta[alert.alert_type]}</p>
              <Badge tone={over ? "danger" : "warning"}>{over ? "Exceeding" : "Within budget"}</Badge>
              {alert.is_resolved && (
                <Badge tone="success">Resolved</Badge>
              )}
            </div>
            <p className="mt-1 text-[12.5px] text-muted">
              Actual {formatCurrency(actual)} vs threshold {formatCurrency(threshold)}
            </p>
          </div>
        </div>
        <span className="text-[11px] tabular text-faint">{formatDate(alert.created_at)}</span>
      </div>
    </li>
  )
}

export function AlertsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["alerts"],
    queryFn: alertsApi.list,
  })

  return (
    <div className="space-y-6">
      <PageHeader
        title="Cost alerts"
        description="Budget and spend anomalies across your projects."
      />
      <Card title="Alerts" bodyClassName="p-0">
        {isLoading ? (
          <div className="space-y-2 p-4">
            {[0, 1].map((i) => (
              <div key={i} className="h-14 animate-pulse rounded-md bg-surface-2" />
            ))}
          </div>
        ) : isError ? (
          <p className="p-4 text-[13px] text-faint">Unable to load cost alerts.</p>
        ) : data && data.length > 0 ? (
          <ul>
            {data.map((a) => (
              <Row key={a.id} alert={a} />
            ))}
          </ul>
        ) : (
          <EmptyState
            icon={<IconAlert className="size-5" />}
            title="No cost alerts"
            description="Budget and spend spikes will appear here. The cost alerts API is not wired up yet."
          />
        )}
      </Card>
    </div>
  )
}