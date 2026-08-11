import { useCallback, useMemo, useRef, useState, type ReactNode } from "react"
import { IconAlert, IconCheck } from "../icons"
import { ToastContext, type ToastInput, type Tone } from "./useToast"

interface Toast extends ToastInput {
  id: number
  tone: Tone
}

const toneStyles: Record<Tone, { ring: string; icon: string; bar: string }> = {
  success: { ring: "border-accent/30", icon: "text-accent-strong", bar: "bg-accent" },
  info: { ring: "border-border-strong", icon: "text-low", bar: "bg-low" },
  error: { ring: "border-critical/30", icon: "text-critical", bar: "bg-critical" },
}

const AUTO_DISMISS_MS = 4000
const MAX_TOASTS = 4

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => {
    setToasts((ts) => ts.filter((t) => t.id !== id))
  }, [])

  const showToast = useCallback(
    (input: ToastInput) => {
      const id = nextId.current++
      const toast: Toast = {
        id,
        title: input.title,
        description: input.description,
        tone: input.tone ?? "success",
      }
      setToasts((ts) => [...ts.slice(-(MAX_TOASTS - 1)), toast])
      window.setTimeout(() => dismiss(id), AUTO_DISMISS_MS)
    },
    [dismiss],
  )

  const value = useMemo(() => ({ showToast }), [showToast])

  return (
    <ToastContext.Provider value={value}>
      {children}

      <div className="pointer-events-none fixed top-4 right-4 z-[100] flex w-[340px] flex-col gap-2">
        {toasts.map((t) => {
          const s = toneStyles[t.tone]
          return (
            <div
              key={t.id}
              role="status"
              className={`pointer-events-auto relative flex items-start gap-3 overflow-hidden rounded-lg border bg-surface px-4 py-3 shadow-lg shadow-black/10 toast-enter ${s.ring}`}
            >
              <span className={`absolute inset-y-0 left-0 w-0.5 ${s.bar}`} />
              <span className={`mt-0.5 ${s.icon}`}>
                {t.tone === "success" ? (
                  <IconCheck className="size-4" />
                ) : (
                  <IconAlert className="size-4" />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-[13px] font-medium text-foreground">{t.title}</p>
                {t.description && (
                  <p className="mt-0.5 text-[12.5px] leading-relaxed text-muted">{t.description}</p>
                )}
              </div>
              <button
                type="button"
                onClick={() => dismiss(t.id)}
                aria-label="Dismiss notification"
                className="text-faint transition-colors hover:text-foreground"
              >
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                  <path d="m2 2 8 8M10 2 2 10" />
                </svg>
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}