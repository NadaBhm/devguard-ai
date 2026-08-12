import { IconCheck, IconX } from "../icons"
import type { RunState } from "../../types/jobs"

interface StepDef {
  key: string
  label: string
  sub?: string
}

const PIPELINE: StepDef[] = [
  { key: "clone", label: "Clone repository" },
  { key: "codesec", label: "Security scan", sub: "Semgrep · Gitleaks · Trivy · Bandit" },
  { key: "gate_1", label: "Gate 1 · approve findings" },
  { key: "infracost", label: "Infrastructure & cost estimate" },
  { key: "gate_2", label: "Gate 2 · approve deployment" },
  { key: "deploy", label: "Generate & apply" },
  { key: "health", label: "Health check" },
  { key: "report", label: "Report" },
]

type Phase = "done" | "active" | "todo" | "skipped"

const PHASE_ORDER: RunState[] = [
  "completed",
  "health_checking",
  "deploying",
  "awaiting_approval_gate_2",
  "infra_generating",
  "awaiting_approval_gate_1",
  "analyzing",
  "cloning",
  "pending",
]

function activeIndex(status: RunState | undefined): number {
  if (!status) return -1
  const i = PHASE_ORDER.indexOf(status)
  if (i === -1) return -1
  return PHASE_ORDER.length - 1 - i
}

const NODE_TO_STEP: Record<string, string> = {
  clone: "clone",
  codesec_agent: "codesec",
  human_gate_1: "gate_1",
  infracost_agent: "infracost",
  human_gate_2: "gate_2",
  deployops_agent: "deploy",
  health_check: "health",
  generate_report: "report",
}

const STEP_NODES: Record<string, string[]> = {
  clone: ["clone"],
  codesec: ["codesec_agent"],
  gate_1: ["human_gate_1"],
  infracost: ["infracost_agent"],
  gate_2: ["human_gate_2"],
  deploy: ["deployops_agent"],
  health: ["health_check"],
  report: ["generate_report"],
}

export function ProgressTimeline({
  status,
  nodesExecuted = [],
  activePhase,
}: {
  status: RunState | undefined
  nodesExecuted?: string[]
  activePhase?: string
}) {
  const activeIdx = activeIndex(status)
  const failed = status === "failed" || status === "rolled_back" || status === "rejected"

  return (
    <ol className="space-y-0">
      {PIPELINE.map((step, i) => {
        const isLast = i === PIPELINE.length - 1
        const stepDone = (STEP_NODES[step.key] ?? []).some((n) => nodesExecuted.includes(n))
        const stepActive = activePhase != null && NODE_TO_STEP[activePhase] === step.key
        let phase: Phase
        if (failed && i >= activeIdx) {
          phase = "skipped"
        } else if (stepDone || i < activeIdx) {
          phase = "done"
        } else if (stepActive || (i === activeIdx && status !== "completed")) {
          phase = "active"
        } else if (status === "completed") {
          phase = "done"
        } else {
          phase = "todo"
        }
        if (status === "rejected" && (step.key === "gate_1" || step.key === "gate_2")) {
          phase = "skipped"
        }

        return (
          <li key={step.key} className="flex gap-3">
            <div className="flex flex-col items-center">
              <span
                className={`flex size-5 shrink-0 items-center justify-center rounded-full border text-[10px] ${
                  phase === "done"
                    ? "border-accent bg-accent/15 text-accent"
                    : phase === "active"
                      ? "border-accent bg-accent/10 text-accent"
                      : phase === "skipped"
                        ? "border-critical/30 bg-critical/10 text-critical"
                        : "border-border bg-surface-2 text-faint"
                }`}
              >
                {phase === "done" ? (
                  <IconCheck className="size-3" />
                ) : phase === "skipped" ? (
                  <IconX className="size-3" />
                ) : phase === "active" ? (
                  <span className="size-1.5 animate-pulse-dot rounded-full bg-accent" />
                ) : null}
              </span>
              {!isLast && (
                <span
                  className={`my-1 w-px flex-1 ${phase === "done" ? "bg-accent/40" : "bg-border"}`}
                />
              )}
            </div>
            <div className="pb-4">
              <p
                className={`text-[12.5px] leading-5 ${
                  phase === "skipped"
                    ? "text-faint line-through"
                    : phase === "done"
                      ? "text-foreground"
                      : phase === "active"
                        ? "font-medium text-accent"
                        : "text-muted"
                }`}
              >
                {step.label}
              </p>
              {step.sub && <p className="text-[11px] text-faint">{step.sub}</p>}
            </div>
          </li>
        )
      })}
    </ol>
  )
}