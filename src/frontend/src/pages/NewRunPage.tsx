import { useState, type FormEvent } from "react"
import { useNavigate } from "react-router-dom"
import { useMutation } from "@tanstack/react-query"
import { jobsApi } from "../api/jobs"
import { Card } from "../components/ui/Card"
import { Button } from "../components/ui/Button"
import { Field, Input } from "../components/ui/Inputs"
import { PageHeader } from "../components/ui/Misc"
import { IconCost, IconDeploy, IconRepo, IconShield } from "../components/icons"
import { ApiError } from "../api/client"

export function NewRunPage() {
  const navigate = useNavigate()
  const [repoUrl, setRepoUrl] = useState("")
  const [commitSha, setCommitSha] = useState("HEAD")
  const [defaultBranch, setDefaultBranch] = useState("main")
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: () =>
      jobsApi.create({
        repo_url: repoUrl.trim(),
        commit_sha: commitSha || "HEAD",
        default_branch: defaultBranch,
      }),
    onSuccess: (res) => {
      if (res.job_id) navigate(`/runs/${res.job_id}`)
    },
  })

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    if (!isValidRepoUrl(repoUrl)) {
      setError("Enter a valid GitHub repository URL, e.g. https://github.com/org/repo")
      return
    }
    mutation.mutate()
  }

  return (
    <div className="mx-auto max-w-2xl space-y-6">
      <PageHeader
        title="New run"
        description="Analyze a public repository for security, infrastructure cost, and deployment readiness."
      />

      <form onSubmit={onSubmit} className="space-y-5">
        <Card title="Repository" description="DevGuard clones the repo and runs the analysis pipeline.">
          <div className="space-y-4">
            <Field label="GitHub URL" hint="Public repository">
              <div className="relative">
                <span className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-faint">
                  <IconRepo className="size-4" />
                </span>
                <Input
                  autoFocus
                  value={repoUrl}
                  onChange={(e) => setRepoUrl(e.target.value)}
                  placeholder="https://github.com/org/repo"
                  className="pl-9 font-mono text-[13px]"
                />
              </div>
            </Field>

            <div className="grid grid-cols-2 gap-4">
              <Field label="Default branch">
                <Input value={defaultBranch} onChange={(e) => setDefaultBranch(e.target.value)} />
              </Field>
              <Field label="Commit" hint="Defaults to HEAD">
                <Input value={commitSha} onChange={(e) => setCommitSha(e.target.value)} className="font-mono" />
              </Field>
            </div>
          </div>
        </Card>

        <Card title="What happens next">
          <p className="text-[13px] leading-relaxed text-muted">
            The pipeline will clone the repository and run three parallel analyses. Each gate pauses
            for your approval before the pipeline continues.
          </p>
          <ul className="mt-4 space-y-2.5">
            <Step icon={<IconShield className="size-4 text-accent" />} title="CodeSec" text="Semgrep, Gitleaks, Trivy and Bandit scan for vulnerabilities and secrets." />
            <Step icon={<IconCost className="size-4 text-accent" />} title="InfraCost" text="Infrastructure proposal with monthly and annual cost estimates." />
            <Step icon={<IconDeploy className="size-4 text-accent" />} title="DeployOps" text="Terraform artifacts, deployment plan and health check." />
          </ul>
        </Card>

        {error && (
          <p className="rounded-md border border-critical/30 bg-critical/10 px-3 py-2 text-[13px] text-critical" role="alert">
            {error}
          </p>
        )}
        {mutation.isError && (
          <p className="rounded-md border border-critical/30 bg-critical/10 px-3 py-2 text-[13px] text-critical" role="alert">
            {mutation.error instanceof ApiError ? mutation.error.detail : "Failed to start the analysis."}
          </p>
        )}

        <div className="flex items-center justify-between">
          <Button type="submit" variant="primary" loading={mutation.isPending}>
            Start analysis
          </Button>
        </div>
      </form>
    </div>
  )
}

function isValidRepoUrl(url: string): boolean {
  try {
    const u = new URL(url)
    return u.protocol === "https:" && u.hostname.endsWith("github.com") && u.pathname.split("/").filter(Boolean).length >= 2
  } catch {
    return false
  }
}

function Step({ icon, title, text }: { icon: React.ReactNode; title: string; text: string }) {
  return (
    <li className="flex items-start gap-3">
      {icon}
      <div>
        <p className="text-[13px] font-medium text-foreground">{title}</p>
        <p className="text-[12.5px] leading-relaxed text-muted">{text}</p>
      </div>
    </li>
  )
}