import { useState, type FormEvent } from "react"
import { useQuery } from "@tanstack/react-query"
import { useAuth } from "../features/auth/useAuth"
import { authApi } from "../api/auth"
import { Card } from "../components/ui/Card"
import { Button } from "../components/ui/Button"
import { Field, Input } from "../components/ui/Inputs"
import { PageHeader } from "../components/ui/Misc"
import { Badge } from "../components/ui/Badge"
import { ApiError } from "../api/client"

export function SettingsPage() {
  const { user, refreshUser } = useAuth()
  const { isLoading } = useQuery({ queryKey: ["me"], queryFn: authApi.me })

  const [form, setForm] = useState({
    first_name: user?.first_name ?? "",
    last_name: user?.last_name ?? "",
    email: user?.email ?? "",
  })
  const [password, setPassword] = useState("")
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
        ...form,
        ...(password ? { password } : {}),
      })
      await refreshUser()
      setPassword("")
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

  if (isLoading || !user) return null

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <PageHeader title="Settings" description="Manage your account and profile details." />

      <Card title="Profile">
        <form onSubmit={onSubmit} className="space-y-4">
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
          <Field label="New password" hint="Leave blank to keep current">
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
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

      <Card title="Account">
        <dl className="space-y-2.5 text-[13px]">
          <div className="flex items-center justify-between">
            <dt className="text-faint">Email</dt>
            <dd className="font-mono text-foreground">{user.email}</dd>
          </div>
          <div className="flex items-center justify-between">
            <dt className="text-faint">Role</dt>
            <dd>
              <Badge tone={user.role === "admin" ? "accent" : "neutral"}>{user.role}</Badge>
            </dd>
          </div>
          <div className="flex items-center justify-between">
            <dt className="text-faint">Verified</dt>
            <dd>{user.is_verified ? <Badge tone="success">Yes</Badge> : <Badge tone="warning">No</Badge>}</dd>
          </div>
        </dl>
      </Card>
    </div>
  )
}