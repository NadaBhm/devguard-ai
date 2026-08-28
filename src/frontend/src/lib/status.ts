import type { Severity } from "../types/results"
import type { RunStatus } from "../types/jobs"

export type BadgeTone = "accent" | "neutral" | "success" | "warning" | "danger" | "info"

export const statusMeta: Record<RunStatus, { label: string; tone: BadgeTone }> = {
  queued: { label: "Queued", tone: "warning" },
  running: { label: "Running", tone: "info" },
  completed: { label: "Completed", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  rolled_back: { label: "Rolled back", tone: "warning" },
  destroyed: { label: "Destroyed", tone: "neutral" },
}

export const runStateMeta: Record<string, { label: string; tone: BadgeTone }> = {
  pending: { label: "Pending", tone: "neutral" },
  cloning: { label: "Cloning repo", tone: "info" },
  analyzing: { label: "Analyzing", tone: "info" },
  awaiting_approval_gate_1: { label: "Awaiting gate 1 approval", tone: "warning" },
  infra_generating: { label: "Generating infrastructure", tone: "info" },
  awaiting_approval_gate_2: { label: "Awaiting gate 2 approval", tone: "warning" },
  deploying: { label: "Deploying", tone: "info" },
  health_checking: { label: "Health check", tone: "info" },
  completed: { label: "Completed", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  rolled_back: { label: "Rolled back", tone: "danger" },
  rejected: { label: "Rejected", tone: "danger" },
}

export function severityTone(severity: Severity): BadgeTone {
  switch (severity) {
    case "critical":
      return "danger"
    case "high":
      return "warning"
    case "medium":
      return "warning"
    default:
      return "neutral"
  }
}

export function severityLabel(severity: Severity): string {
  return severity.charAt(0).toUpperCase() + severity.slice(1)
}