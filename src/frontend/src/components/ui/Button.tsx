import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react"
import { Link } from "react-router-dom"

type Variant = "primary" | "secondary" | "ghost" | "danger" | "accent-outline"
type Size = "sm" | "md"

const variants: Record<Variant, string> = {
  primary:
    "bg-accent text-[#05231b] hover:bg-accent-strong font-medium shadow-[0_1px_0_rgba(255,255,255,0.08)]",
  "accent-outline": "border border-accent/40 text-accent hover:bg-accent/10",
  secondary:
    "bg-surface-2 text-foreground border border-border hover:border-border-strong hover:bg-raised",
  ghost: "text-muted hover:text-foreground hover:bg-surface-2",
  danger: "bg-critical/15 text-critical border border-critical/30 hover:bg-critical/25",
}

const sizes: Record<Size, string> = {
  sm: "h-8 px-3 text-[13px] gap-1.5",
  md: "h-10 px-3.5 text-sm gap-2",
}

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant
  size?: Size
  loading?: boolean
  icon?: ReactNode
  to?: string
}

export const Button = forwardRef<HTMLButtonElement, Props>(
  ({ variant = "secondary", size = "md", loading, icon, children, className, disabled, to, ...rest }, ref) => {
    const cls = `inline-flex items-center justify-center rounded-md font-medium transition-colors duration-150 focus-visible:outline-2 focus-visible:outline-accent select-none disabled:opacity-45 disabled:pointer-events-none ${variants[variant]} ${sizes[size]} ${className ?? ""}`
    if (to) {
      return (
        <Link to={to} className={cls}>
          {loading ? <Spinner size={14} /> : icon}
          {children}
        </Link>
      )
    }
    return (
      <button
        ref={ref}
        disabled={disabled || loading}
        className={cls}
        {...rest}
      >
        {loading ? <Spinner size={14} /> : icon}
        {children}
      </button>
    )
  },
)
Button.displayName = "Button"

export function Spinner({ size = 16, className }: { size?: number; className?: string }) {
  return (
    <svg
      className={`animate-spin ${className ?? ""}`}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" strokeOpacity="0.2" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  )
}