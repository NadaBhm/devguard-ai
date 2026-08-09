import type { ReactNode } from "react"

type Tone = "accent" | "neutral" | "success" | "warning" | "danger" | "info"

const tones: Record<Tone, string> = {
  accent: "text-accent bg-accent/10 border-accent/20",
  neutral: "text-muted bg-surface-2 border-border",
  success: "text-accent-strong bg-accent/10 border-accent/20",
  warning: "text-medium bg-medium/12 border-medium/25",
  danger: "text-critical bg-critical/12 border-critical/25",
  info: "text-low bg-low/10 border-low/20",
}

export function Badge({
  tone = "neutral",
  children,
  dot,
  className,
}: {
  tone?: Tone
  children: ReactNode
  dot?: boolean
  className?: string
}) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-[5px] border px-2 py-0.5 text-[11px] font-medium leading-5 whitespace-nowrap ${tones[tone]} ${className ?? ""}`}
    >
      {dot && <span className="size-1.5 rounded-full bg-current" />}
      {children}
    </span>
  )
}