import { useMemo, useState } from "react"
import type { CodeSecFinding, Scanner, Severity } from "../../types/results"
import { SeverityBadge } from "./SeverityBadge"
import { Badge } from "../ui/Badge"

const SCANNERS: Scanner[] = ["semgrep", "gitleaks", "trivy", "bandit"]
const SEVERITIES: Severity[] = ["critical", "high", "medium", "low"]

function FindingRow({ finding }: { finding: CodeSecFinding }) {
  const [open, setOpen] = useState(false)
  return (
    <li className="border-b border-border last:border-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-2/60"
        aria-expanded={open}
      >
        <SeverityBadge severity={finding.severity} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-medium text-foreground">{finding.rule_title}</p>
          <p className="truncate font-mono text-[11.5px] text-faint">
            {finding.file_path}
            {finding.line_number != null ? `:${finding.line_number}` : ""}
          </p>
        </div>
        <code className="hidden rounded border border-border bg-surface-2 px-1.5 py-0.5 font-mono text-[10.5px] text-muted sm:block">
          {finding.rule_id}
        </code>
      </button>
      {open && (
        <div className="border-t border-border bg-canvas/60 px-4 py-3">
          <p className="text-[13px] leading-relaxed text-muted">{finding.description}</p>
          {finding.remediation_hint && (
            <div className="mt-3 rounded-md border border-accent/20 bg-accent/[0.06] px-3 py-2">
              <p className="text-[11px] font-medium uppercase tracking-wide text-accent">Remediation</p>
              <p className="mt-1 text-[13px] leading-relaxed text-foreground">{finding.remediation_hint}</p>
            </div>
          )}
        </div>
      )}
    </li>
  )
}

export function CodeSecTab({ findings }: { findings: CodeSecFinding[] }) {
  const [severity, setSeverity] = useState<Severity | "all">("all")
  const [scanner, setScanner] = useState<Scanner | "all">("all")

  const filtered = useMemo(
    () =>
      findings.filter(
        (f) => (severity === "all" || f.severity === severity) && (scanner === "all" || f.scanner === scanner),
      ),
    [findings, severity, scanner],
  )

  const counts = useMemo(() => {
    const c: Record<Severity, number> = { critical: 0, high: 0, medium: 0, low: 0 }
    for (const f of findings) c[f.severity] += 1
    return c
  }, [findings])

  if (findings.length === 0) {
    return <Empty text="No findings recorded for this run yet." />
  }

  return (
    <div>
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3">
        {SEVERITIES.map((s) => {
          const n = counts[s]
          const active = severity === s
          return (
            <button
              key={s}
              type="button"
              onClick={() => setSeverity(active ? "all" : s)}
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11.5px] font-medium transition-colors ${
                active
                  ? "border-border-strong bg-raised text-foreground"
                  : "border-border bg-transparent text-muted hover:text-foreground"
              }`}
            >
              <span className={`size-1.5 rounded-full ${sevColor(s)}`} />
              {s}
              <span className="tabular text-faint">{n}</span>
            </button>
          )
        })}
        <div className="mx-2 h-4 w-px bg-border" />
        <select
          value={scanner}
          onChange={(e) => setScanner(e.target.value as Scanner | "all")}
          className="h-7 rounded-md border border-border bg-surface-2 px-2 text-[12px] text-foreground focus:outline-none"
          aria-label="Filter by scanner"
        >
          <option value="all">All scanners</option>
          {SCANNERS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <span className="ml-auto text-[12px] text-faint">
          {filtered.length} of {findings.length}
        </span>
      </div>

      {filtered.length === 0 ? (
        <Empty text="No findings match the current filters." />
      ) : (
        <ul>
          {filtered.map((f) => (
            <FindingRow key={f.id} finding={f} />
          ))}
        </ul>
      )}
    </div>
  )
}

function Empty({ text }: { text: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <Badge tone="success" dot>
        Clean
      </Badge>
      <p className="mt-3 max-w-sm text-[13px] text-muted">{text}</p>
    </div>
  )
}

function sevColor(s: Severity): string {
  switch (s) {
    case "critical":
      return "bg-critical"
    case "high":
      return "bg-high"
    case "medium":
      return "bg-medium"
    default:
      return "bg-low"
  }
}