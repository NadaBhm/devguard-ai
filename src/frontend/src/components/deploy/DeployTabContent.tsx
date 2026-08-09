import type { Deployment, DeploymentStatus } from "../../types/results"
import { formatCurrency, formatDate } from "../../lib/format"
import { Badge } from "../ui/Badge"
import { IconCheck, IconX } from "../icons"

const statusMeta: Record<DeploymentStatus, { label: string; tone: "accent" | "neutral" | "success" | "warning" | "danger" }> = {
  pending: { label: "Pending", tone: "neutral" },
  applying: { label: "Applying", tone: "warning" },
  succeeded: { label: "Succeeded", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  rolled_back: { label: "Rolled back", tone: "danger" },
}

const envMeta = {
  dev: { label: "Development", dot: "bg-low" },
  staging: { label: "Staging", dot: "bg-medium" },
  prod: { label: "Production", dot: "bg-accent" },
}

export function DeployTab({ deployments }: { deployments: Deployment[] }) {
  if (deployments.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <p className="max-w-sm text-[13px] text-muted">No deployment records for this run yet.</p>
      </div>
    )
  }

  return (
    <div className="space-y-3 p-4">
      {deployments.map((d) => {
        const meta = statusMeta[d.status]
        const envMetaFor = envMeta[d.environment] ?? envMeta.dev
        return (
          <div key={d.id} className="rounded-lg border border-border bg-surface-2">
            <div className="flex items-center gap-3 px-4 py-3">
              <span className={`size-2 rounded-full ${envMetaFor.dot}`} />
              <div className="min-w-0 flex-1">
                <p className="text-[13px] font-medium text-foreground">{envMetaFor.label}</p>
                <p className="font-mono text-[11.5px] text-faint">{d.aws_region}</p>
              </div>
              <Badge tone={meta.tone}>{meta.label}</Badge>
              {d.status === "succeeded" && <IconCheck className="size-4 text-accent" />}
              {d.status === "failed" && <IconX className="size-4 text-critical" />}
            </div>
            <dl className="grid grid-cols-2 border-t border-border px-4 py-2.5 text-[12.5px] gap-x-6 gap-y-1.5">
              <div className="flex justify-between">
                <dt className="text-faint">Applied</dt>
                <dd className="font-medium text-foreground">{d.applied_at ? formatDate(d.applied_at) : "—"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-faint">Cost / mo</dt>
                <dd className="font-medium tabular text-foreground">{d.cost_total_monthly != null ? formatCurrency(d.cost_total_monthly) : "—"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-faint">Terraform</dt>
                <dd className="font-mono text-muted">{d.terraform_version ?? "—"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-faint">State ID</dt>
                <dd className="max-w-[180px] truncate font-mono text-muted">{d.terraform_state_id ?? "—"}</dd>
              </div>
              {d.rollback_reason && (
                <div className="col-span-2 flex justify-between">
                  <dt className="text-faint">Rollback reason</dt>
                  <dd className="text-critical">{d.rollback_reason}</dd>
                </div>
              )}
            </dl>
          </div>
        )
      })}
    </div>
  )
}