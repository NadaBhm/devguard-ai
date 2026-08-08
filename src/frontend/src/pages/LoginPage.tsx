import { useState, type FormEvent } from "react"
import { Link, useNavigate } from "react-router-dom"
import { useAuth } from "../features/auth/useAuth"
import { AuthShell } from "../components/auth/AuthShell"
import { Button } from "../components/ui/Button"
import { Field, Input } from "../components/ui/Inputs"
import { ApiError } from "../api/client"

function friendlyAuthError(err: unknown): string {
  const detail = err instanceof ApiError ? err.detail : ""
  switch (detail) {
    case "Incorrect email or password":
      return "The email or password is incorrect."
    case "Inactive user":
      return "This account isn't verified yet. Contact your administrator to activate it."
    default:
      return err instanceof ApiError && detail
        ? detail
        : "Unable to sign in. Please try again."
  }
}

export function LoginPage() {
  const { login } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login({ email, password })
      navigate("/", { replace: true })
    } catch (err) {
      setError(friendlyAuthError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <AuthShell
      title="Sign in to DevGuard"
      subtitle="Access your repository analysis and risk overview."
      footer={
        <>
          Don&apos;t have an account?{" "}
          <Link to="/register" className="font-medium text-accent hover:text-accent-strong">
            Create one
          </Link>
        </>
      }
    >
      <form onSubmit={onSubmit} className="space-y-4">
        <Field label="Email" htmlFor="email">
          <Input
            id="email"
            type="email"
            autoComplete="email"
            required
            placeholder="you@company.com"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </Field>
        <Field label="Password" htmlFor="password">
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            placeholder="Enter your password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </Field>

        {error && (
          <p className="rounded-md border border-critical/30 bg-critical/10 px-3 py-2 text-[13px] text-critical" role="alert">
            {error}
          </p>
        )}

        <Button type="submit" variant="primary" className="w-full" loading={submitting}>
          Sign in
        </Button>
      </form>
    </AuthShell>
  )
}