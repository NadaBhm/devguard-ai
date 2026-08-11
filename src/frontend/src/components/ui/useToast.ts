import { createContext, useContext } from "react"

export type Tone = "success" | "info" | "error"

export interface ToastInput {
  title: string
  description?: string
  tone?: Tone
}

export interface ToastContextValue {
  showToast: (input: ToastInput) => void
}

export const ToastContext = createContext<ToastContextValue | null>(null)

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error("useToast must be used within <ToastProvider>")
  return ctx
}