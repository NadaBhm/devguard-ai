import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { deploymentsApi, type DeploymentItem } from "../api/deployments"
import { Card } from "../components/ui/Card"
import { Badge } from "../components/ui/Badge"
import { EmptyState } from "../components/ui/Misc"
import { PageHeader } from "../components/ui/Misc"
import { IconArrowRight, IconDeploy, IconExternal } from "../components/icons"
import { formatCurrency, formatDate } from "../lib/format"

const statusMeta: Record<string, { label: string; tone: "accent" | "neutral" | "success" | "warning" | "danger" }> = {
  pending: { label: "Pending", tone: "neutral" },
  applying: { label: "Applying", tone: "warning" },
  succeeded: { label: "Succeeded", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  rolled_back: { label: "Rolled back", tone: "danger" },
}

const envMeta: Record<string, { label: string; dot: string }> = {
  dev: { label: "Development", dot: "bg-low" },
  staging: { label: "Staging", dot: "bg-medium" },
  prod: { label: "Production", dot: "bg-accent" },
}

function DeploymentRow({ d }: { d: DeploymentItem }) {
  const meta = statusMeta[d.status] ?? statusMeta.pending
  const env = envMeta[d.environment] ?? envMeta.dev
  return (
    <li className="border-b border-border px-4 py-3 last:border-0">
      <div className="flex items-center gap-3">
        <span className={`size-2 shrink-0 rounded-full ${env.dot}`} />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <p className="truncate text-[13px] font-medium text-foreground">{d.repo_name ?? "Unknown repo"}</p>
            <span className="hidden text-[11px] text-faint md:inline">{env.label}</span>
          </div>
          <p className="mt-0.5 flex items-center gap-1 font-mono text-[11.5px] text-faint">
            {d.repo_url?.replace(/^https?:\/\//, "") ?? d.aws_region}
            {d.repo_url && <IconExternal className="size-3 shrink-0" />}
          </p>
        </div>
        <dl className="hidden gap-x-6 text-right text-[11.5px] sm:flex">
          <div>
            <dt className="text-faint">Cost / mo</dt>
            <dd className="font-medium tabular text-foreground">
              {d.cost_total_monthly != null ? formatCurrency(d.cost_total_monthly) : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-faint">Applied</dt>
            <dd className="font-medium text-foreground">{d.applied_at ? formatDate(d.applied_at) : "—"}</dd>
          </div>
        </dl>
        <div className="flex items-center gap-2">
          <Badge tone={meta.tone}>{meta.label}</Badge>
          <Link
            to={`/runs/${d.run_id}`}
            className="flex size-7 items-center justify-center rounded-md border border-border text-muted transition-colors hover:border-border-strong hover:text-foreground"
            title={`Open run ${d.run_id}`}
          >
            <IconArrowRight className="size-3.5" />
          </Link>
        </div>
      </div>
      {d.rollback_reason && (
        <p className="mt-1.5 pl-5 text-[12px] text-critical">Rollback: {d.rollback_reason}</p>
      )}
    </li>
  )
}

export function DeploymentsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["deployments"],
    queryFn: deploymentsApi.list,
  })

  return (
    <div className="space-y-6">
      <PageHeader
        title="Deployments"
        description="Infrastructure you've shipped across projects, with the ability to roll back to a previous revision from each run."
      />
      <Card title="All deployments" bodyClassName="p-0">
        {isLoading ? (
          <div className="space-y-2 p-4">
            {[0, 1].map((i) => (
              <div key={i} className="h-14 animate-pulse rounded-md bg-surface-2" />
            ))}
          </div>
        ) : isError ? (
          <p className="p-4 text-[13px] text-faint">Unable to load deployments.</p>
        ) : data && data.length > 0 ? (
          <ul>
            {data.map((d) => (
              <DeploymentRow key={d.id} d={d} />
            ))}
          </ul>
        ) : (
          <EmptyState
            icon={<IconDeploy className="size-5" />}
            title="No deployments yet"
            description="Infrastructure shipped through an analysis run will appear here."
          />
        )}
      </Card>
    </div>
  )
}
