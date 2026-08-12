import type { InfracostEstimate } from "../../types/results"
import type { InfracostIteration, InfraCostResult } from "../../types/jobs"
import { formatCurrency } from "../../lib/format"
import { Badge } from "../ui/Badge"

function total(estimates: InfracostEstimate[]): number {
  return estimates.reduce((sum, e) => sum + Number(e.monthly_cost_usd || 0), 0)
}

function annualTotal(estimates: InfracostEstimate[]): number {
  return estimates.reduce((sum, e) => sum + Number(e.annual_cost_usd || 0), 0)
}

function iterationCost(it: InfracostIteration): number | null {
  const monthly = it.result?.cost_estimate?.monthly_cost_usd
  return typeof monthly === "number" ? monthly : null
}

export function InfraCostTab({
  estimates,
  infracost,
  iterations,
}: {
  estimates: InfracostEstimate[]
  infracost?: InfraCostResult | null
  iterations?: InfracostIteration[]
}) {
  const monthly = total(estimates)
  const annual = annualTotal(estimates)
  const arch = infracost?.architecture_recommendation

  const hasAny = estimates.length > 0 || arch || infracost?.justification || (iterations?.length ?? 0) > 0

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
      {(iterations?.length ?? 0) > 0 && (
        <div className="overflow-hidden rounded-lg border border-border">
          <div className="flex items-center justify-between border-b border-border bg-surface-2 px-4 py-2.5">
            <p className="text-[11px] font-medium uppercase tracking-wider text-faint">
              Regeneration history
            </p>
            <Badge tone="accent">{iterations!.length} round{iterations!.length > 1 ? "s" : ""}</Badge>
          </div>
          <ul className="divide-y divide-border">
            {iterations!.map((it) => (
              <li key={it.iteration} className="flex items-start gap-3 px-4 py-3">
                <span className="mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-md border border-border bg-surface-2 font-mono text-[11px] text-muted">
                  #{it.iteration}
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-[12.5px] text-foreground">
                    <span className="text-faint">Prompt: </span>
                    {it.prompt}
                  </p>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-[12px] text-muted">
                    {it.result?.architecture_recommendation && (
                      <code className="rounded border border-border bg-canvas px-1.5 py-0.5 font-mono text-[11.5px] text-accent">
                        {it.result.architecture_recommendation}
                      </code>
                    )}
                    {iterationCost(it) != null && (
                      <span className="tabular">{formatCurrency(iterationCost(it)!)}/mo</span>
                    )}
                  </div>
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

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