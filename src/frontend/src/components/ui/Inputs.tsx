import { forwardRef, type InputHTMLAttributes, type ReactNode, type SelectHTMLAttributes } from "react"

const base =
  "w-full rounded-md bg-surface-2 border border-border px-3 text-sm text-foreground placeholder:text-faint transition-colors focus:outline-none focus:border-border-strong focus:ring-2 focus:ring-accent/20 disabled:opacity-50"

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...rest }, ref) => (
    <input ref={ref} className={`${base} h-10 ${className ?? ""}`} {...rest} />
  ),
)
Input.displayName = "Input"

export const Textarea = forwardRef<HTMLTextAreaElement, InputHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...rest }, ref) => (
    <textarea ref={ref} className={`${base} min-h-[96px] py-2.5 ${className ?? ""}`} {...rest} />
  ),
)
Textarea.displayName = "Textarea"

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, children, ...rest }, ref) => (
    <select ref={ref} className={`${base} h-10 appearance-none pr-8 bg-no-repeat bg-[right_0.5rem_center] bg-[url('data:image/svg+xml;charset=utf-8,%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20width%3D%2210%22%20height%3D%226%22%20viewBox%3D%220%200%2010%206%22%3E%3Cpath%20d%3D%22M1%201l4%204%204-4%22%20stroke%3D%22%235d6a7a%22%20stroke-width%3D%221.5%22%20fill%3D%22none%22%2F%3E%3C%2Fsvg%3E')] ${className ?? ""}`} {...rest}>
      {children}
    </select>
  ),
)
Select.displayName = "Select"

export function Field({
  label,
  hint,
  error,
  children,
  htmlFor,
}: {
  label: string
  hint?: string
  error?: string
  htmlFor?: string
  children: ReactNode
}) {
  return (
    <label className="block space-y-1.5" htmlFor={htmlFor}>
      <span className="flex items-baseline justify-between">
        <span className="text-[13px] font-medium text-foreground">{label}</span>
        {hint && <span className="text-[11px] text-faint">{hint}</span>}
      </span>
      {children}
      {error && <span className="block text-[12px] text-critical">{error}</span>}
    </label>
  )
}