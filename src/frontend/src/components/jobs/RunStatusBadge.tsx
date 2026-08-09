import { Badge } from "../ui/Badge"
import { runStateMeta, statusMeta, type BadgeTone } from "../../lib/status"
import type { RunStatus } from "../../types/jobs"

export function RunStatusBadge({
  status,
  orchestratorStatus,
}: {
  status: RunStatus
  orchestratorStatus?: string | null
}) {
  const state = orchestratorStatus && runStateMeta[orchestratorStatus]
  if (state) return <Badge tone={state.tone}>{state.label}</Badge>
  const fallback = statusMeta[status] ?? { label: status, tone: "neutral" as BadgeTone }
  return <Badge tone={fallback.tone}>{fallback.label}</Badge>
}