import type { ReactNode } from "react"
import { IconShield } from "../icons"

export function AuthShell({
  title,
  subtitle,
  children,
  footer,
}: {
  title: string
  subtitle: string
  children: ReactNode
  footer: ReactNode
}) {
  return (
    <div className="flex min-h-screen">
      <div className="relative hidden w-[42%] flex-col justify-between overflow-hidden border-r border-border bg-gradient-to-b from-[#0e141d] to-[#0a1412] lg:flex">
        <div className="flex items-center gap-3 px-10 pt-10">
          <div className="flex size-9 items-center justify-center rounded-lg border border-border bg-surface">
            <IconShield className="size-5 text-accent" />
          </div>
          <div className="leading-tight">
            <p className="text-sm font-semibold tracking-tight text-foreground">DevGuard AI</p>
            <p className="text-[11px] text-faint">automated security, cost & deploy analysis</p>
          </div>
        </div>

        <div className="px-10 pb-16">
          <p className="max-w-sm text-2xl font-semibold leading-snug tracking-tight text-foreground">
            Every pull request analyzed before it ships.
          </p>
          <p className="mt-3 max-w-sm text-sm leading-relaxed text-muted">
            CodeSec finds vulnerabilities, InfraCost predicts infrastructure spend, and DeployOps
            generates the Terraform to launch it — with human gates at every decision point.
          </p>
        </div>
      </div>

      <div className="flex flex-1 items-start justify-center px-6 pt-[10vh] pb-16 sm:items-center sm:pt-0">
        <div className="w-full max-w-sm">
          <div className="mb-8 flex items-center gap-2.5 lg:hidden">
            <div className="flex size-8 items-center justify-center rounded-lg border border-border bg-surface">
              <IconShield className="size-4.5 text-accent" />
            </div>
            <span className="text-sm font-semibold tracking-tight text-foreground">DevGuard AI</span>
          </div>

          <h1 className="text-xl font-semibold tracking-tight text-foreground">{title}</h1>
          <p className="mt-1 text-sm text-faint">{subtitle}</p>

          <div className="mt-8">{children}</div>

          <div className="mt-8 border-t border-border pt-5 text-center text-[13px] text-muted">
            {footer}
          </div>
        </div>
      </div>
    </div>
  )
}