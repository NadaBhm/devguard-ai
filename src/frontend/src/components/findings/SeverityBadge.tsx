import type { Severity } from "../../types/results"
import { severityLabel, severityTone } from "../../lib/status"
import { Badge } from "../ui/Badge"

export function SeverityBadge({ severity }: { severity: Severity }) {
  return <Badge tone={severityTone(severity)}>{severityLabel(severity)}</Badge>
}