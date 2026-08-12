import { useState, type FormEvent } from "react"
import { useAuth } from "../features/auth/useAuth"
import { authApi } from "../api/auth"
import { Card } from "../components/ui/Card"
import { Button } from "../components/ui/Button"
import { Field, Input } from "../components/ui/Inputs"
import { Badge } from "../components/ui/Badge"
import { PageHeader } from "../components/ui/Misc"
import { ApiError } from "../api/client"

export function SettingsPage() {
  const { user, refreshUser } = useAuth()
  const [password, setPassword] = useState("")
  const [confirm, setConfirm] = useState("")
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setMessage(null)
    if (password.length < 8) {
      setMessage({ kind: "error", text: "Password must be at least 8 characters." })
      return
    }
    if (password !== confirm) {
      setMessage({ kind: "error", text: "Passwords do not match." })
      return
    }
    setSaving(true)
    try {
      await authApi.updateMe({ password })
      await refreshUser()
      setPassword("")
      setConfirm("")
      setMessage({ kind: "ok", text: "Password updated." })
    } catch (err) {
      setMessage({
        kind: "error",
        text: err instanceof ApiError ? err.detail : "Could not update your password.",
      })
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <PageHeader title="Settings" description="Manage your account and security." />

      <Card title="Change password" description="Set a new password for your account.">
        <form onSubmit={onSubmit} className="space-y-4">
          <Field label="New password" hint="Min 8 characters">
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </Field>
          <Field label="Confirm new password">
            <Input
              type="password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
              autoComplete="new-password"
            />
          </Field>

          {message && (
            <p className={`text-[13px] ${message.kind === "ok" ? "text-accent" : "text-critical"}`} role="status">
              {message.text}
            </p>
          )}

          <div className="flex justify-end">
            <Button type="submit" variant="primary" loading={saving}>
              Update password
            </Button>
          </div>
        </form>
      </Card>

      {user && (
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
      )}
    </div>
  )
}
