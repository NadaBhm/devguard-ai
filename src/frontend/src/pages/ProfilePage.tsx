import { useState, type FormEvent } from "react"
import { useQuery } from "@tanstack/react-query"
import { useAuth } from "../features/auth/useAuth"
import { authApi } from "../api/auth"
import { Card } from "../components/ui/Card"
import { Button } from "../components/ui/Button"
import { Field, Input } from "../components/ui/Inputs"
import { PageHeader } from "../components/ui/Misc"
import { ApiError } from "../api/client"
import { formatCurrency, formatDateLong } from "../lib/format"

export function ProfilePage() {
  const { user, refreshUser } = useAuth()
  const statsQuery = useQuery({
    queryKey: ["me", "stats"],
    queryFn: authApi.stats,
  })

  const [form, setForm] = useState({
    username: user?.username ?? "",
    first_name: user?.first_name ?? "",
    last_name: user?.last_name ?? "",
    email: user?.email ?? "",
  })
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null)

  function update(key: keyof typeof form, value: string) {
    setForm((f) => ({ ...f, [key]: value }))
  }

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setSaving(true)
    setMessage(null)
    try {
      await authApi.updateMe({
        username: form.username.trim() || undefined,
        first_name: form.first_name,
        last_name: form.last_name,
        email: form.email,
      })
      await refreshUser()
      setMessage({ kind: "ok", text: "Profile updated." })
    } catch (err) {
      setMessage({
        kind: "error",
        text: err instanceof ApiError ? err.detail : "Could not update your profile.",
      })
    } finally {
      setSaving(false)
    }
  }

  if (!user) return null

  const stats = statsQuery.data
  const statCards = [
    { label: "Projects", value: stats ? String(stats.total_projects) : "—" },
    { label: "Total runs", value: stats ? String(stats.total_runs) : "—" },
    { label: "Findings", value: stats ? String(stats.total_findings) : "—" },
    { label: "Deployments", value: stats ? String(stats.total_deployments) : "—" },
    { label: "Est. spend / mo", value: stats ? formatCurrency(stats.est_monthly_cost) : "—" },
    {
      label: "Member since",
      value: stats ? formatDateLong(stats.member_since) : "—",
    },
  ]

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <PageHeader title="Profile" description="Update your username, name and email." />

      <Card title="Profile">
        <form onSubmit={onSubmit} className="space-y-4">
          <Field label="Username" hint="Choose a unique handle">
            <Input
              value={form.username}
              onChange={(e) => update("username", e.target.value)}
              placeholder="your_username"
              autoComplete="username"
            />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="First name">
              <Input value={form.first_name} onChange={(e) => update("first_name", e.target.value)} />
            </Field>
            <Field label="Last name">
              <Input value={form.last_name} onChange={(e) => update("last_name", e.target.value)} />
            </Field>
          </div>
          <Field label="Email">
            <Input type="email" value={form.email} onChange={(e) => update("email", e.target.value)} />
          </Field>

          {message && (
            <p className={`text-[13px] ${message.kind === "ok" ? "text-accent" : "text-critical"}`} role="status">
              {message.text}
            </p>
          )}

          <div className="flex justify-end">
            <Button type="submit" variant="primary" loading={saving}>
              Save
            </Button>
          </div>
        </form>
      </Card>

      <Card title="Your activity" description="Usage stats across your repositories.">
        {statsQuery.isLoading ? (
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <div key={i} className="h-16 animate-pulse rounded-md bg-surface-2" />
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
            {statCards.map((s) => (
              <div key={s.label} className="rounded-lg border border-border bg-surface-2 px-4 py-3">
                <p className="text-[11px] font-medium uppercase tracking-wider text-faint">{s.label}</p>
                <p className="mt-1 truncate text-lg font-semibold tabular tracking-tight text-foreground">
                  {s.value}
                </p>
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  )
}
