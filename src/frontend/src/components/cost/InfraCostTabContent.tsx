import type { InfracostEstimate } from "../../types/results"
import type { InfraCostResult } from "../../types/jobs"
import { formatCurrency } from "../../lib/format"
import { Badge } from "../ui/Badge"

function total(estimates: InfracostEstimate[]): number {
  return estimates.reduce((sum, e) => sum + Number(e.monthly_cost_usd || 0), 0)
}

function annualTotal(estimates: InfracostEstimate[]): number {
  return estimates.reduce((sum, e) => sum + Number(e.annual_cost_usd || 0), 0)
}

export function InfraCostTab({ estimates, infracost }: { estimates: InfracostEstimate[]; infracost?: InfraCostResult | null }) {
  const monthly = total(estimates)
  const annual = annualTotal(estimates)
  const arch = infracost?.architecture_recommendation

  const hasAny = estimates.length > 0 || arch || infracost?.justification

  if (!hasAny) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <p className="max-w-sm text-[13px] text-muted">
          No cost estimates recorded for this run yet.
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-4 p-4">
      <div className="grid grid-cols-2 gap-3">
        <div className="rounded-lg border border-border bg-surface-2 p-4">
          <p className="text-[11px] font-medium uppercase tracking-wider text-faint">Estimated monthly</p>
          <p className="mt-1 text-2xl font-semibold tabular tracking-tight text-foreground">{formatCurrency(monthly)}</p>
        </div>
        <div className="rounded-lg border border-border bg-surface-2 p-4">
          <p className="text-[11px] font-medium uppercase tracking-wider text-faint">Annual projection</p>
          <p className="mt-1 text-2xl font-semibold tabular tracking-tight text-foreground">{formatCurrency(annual)}</p>
        </div>
      </div>

      {arch && (
        <div className="flex items-center gap-2.5 rounded-lg border border-border bg-surface-2 px-3.5 py-2.5">
          <span className="text-[12px] text-faint">Recommended architecture</span>
          <code className="rounded border border-border bg-canvas px-1.5 py-0.5 font-mono text-[12px] text-accent">
            {arch}
          </code>
          {infracost?.justification && (
            <span className="ml-auto hidden text-[12px] text-faint sm:block">
              {infracost.justification}
            </span>
          )}
        </div>
      )}

      {estimates.length > 0 && (
        <div className="overflow-hidden rounded-lg border border-border">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border bg-surface-2 text-left text-[11px] uppercase tracking-wider text-faint">
                <th className="px-4 py-2.5 font-medium">Resource</th>
                <th className="px-4 py-2.5 font-medium">Type</th>
                <th className="px-4 py-2.5 text-right font-medium">Monthly</th>
                <th className="px-4 py-2.5 text-right font-medium">Annual</th>
                <th className="px-4 py-2.5 font-medium">Confidence</th>
              </tr>
            </thead>
            <tbody>
              {estimates.map((e) => (
                <tr key={e.id} className="border-b border-border last:border-0 hover:bg-surface-2/60">
                  <td className="px-4 py-2.5 font-mono text-[12.5px] text-foreground">{e.resource_name}</td>
                  <td className="px-4 py-2.5 text-muted">{e.resource_type}</td>
                  <td className="px-4 py-2.5 text-right font-medium tabular text-foreground">{formatCurrency(e.monthly_cost_usd)}</td>
                  <td className="px-4 py-2.5 text-right tabular text-muted">{formatCurrency(e.annual_cost_usd)}</td>
                  <td className="px-4 py-2.5">
                    {e.confidence_level && <Badge tone="accent">{e.confidence_level}</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}