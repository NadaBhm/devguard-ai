import type { HTMLAttributes, ReactNode } from "react"

interface CardProps extends HTMLAttributes<HTMLDivElement> {
  title?: string
  description?: string
  actions?: ReactNode
  bodyClassName?: string
}

export function Card({ title, description, actions, bodyClassName, className, children, ...rest }: CardProps) {
  return (
    <section
      className={`rounded-lg border border-border bg-surface ${className ?? ""}`}
      {...rest}
    >
      {(title || actions) && (
        <header className="flex items-baseline justify-between gap-4 border-b border-border px-4 py-3">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-medium text-foreground">{title}</h2>}
            {description && <p className="mt-0.5 text-[13px] text-faint">{description}</p>}
          </div>
          {actions}
        </header>
      )}
      <div className={bodyClassName ?? "p-4"}>{children}</div>
    </section>
  )
}

export function Surface({ children, className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div className={`rounded-lg border border-border bg-surface ${className ?? ""}`} {...rest}>
      {children}
    </div>
  )
}