import { Badge } from "../ui/Badge"

const MODE_LABELS = {
  real: "Live agents",
  mixed: "Mixed mode",
  mock: "Demo data",
} as const

const MODE_TONES = {
  real: "success",
  mixed: "warning",
  mock: "info",
} as const

export function ModeBadge({ mode }: { mode?: "real" | "mixed" | "mock" }) {
  if (!mode || mode === "real") return null
  return (
    <Badge tone={MODE_TONES[mode]}>
      {MODE_LABELS[mode]}
    </Badge>
  )
}