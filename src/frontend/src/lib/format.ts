export function formatCurrency(value: string | number | null | undefined): string {
  const n = Number(value ?? 0)
  if (!Number.isFinite(n)) return "$0"
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(n)
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || Number.isNaN(seconds)) return "—"
  if (seconds < 60) return `${Math.round(seconds)}s`
  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)
  if (m < 60) return `${m}m ${s}s`
  const h = Math.floor(m / 60)
  return `${h}h ${m % 60}m`
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return "—"
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(d)
}

export function formatDateLong(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return "—"
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(d)
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—"
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return "—"
  const diff = Date.now() - d.getTime()
  const seconds = Math.round(diff / 1000)
  const minute = 60
  const hour = minute * 60
  const day = hour * 24
  if (seconds < 15) return "just now"
  if (seconds < minute) return `${seconds}s ago`
  if (seconds < hour) return `${Math.round(seconds / minute)}m ago`
  if (seconds < day) return `${Math.round(seconds / hour)}h ago`
  return `${Math.round(seconds / day)}d ago`
}

export function shortId(id: string | null | undefined): string {
  if (!id) return "—"
  return id.length > 10 ? id.slice(0, 8) : id
}