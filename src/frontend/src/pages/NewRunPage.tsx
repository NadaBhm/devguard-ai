import { useState, type ChangeEvent, type FormEvent } from "react"
import { useNavigate } from "react-router-dom"
import { useMutation } from "@tanstack/react-query"
import { jobsApi } from "../api/jobs"
import { Card } from "../components/ui/Card"
import { Button } from "../components/ui/Button"
import { Field, Input } from "../components/ui/Inputs"
import { PageHeader } from "../components/ui/Misc"
import { IconCost, IconDeploy, IconRepo, IconShield } from "../components/icons"
import { ApiError } from "../api/client"

type SourceType = "github" | "gitlab" | "local_folder"

export function NewRunPage() {
  const navigate = useNavigate()
  const [sourceType, setSourceType] = useState<SourceType>("github")
  const [repoUrl, setRepoUrl] = useState("")
  const [commitSha, setCommitSha] = useState("HEAD")
  const [defaultBranch, setDefaultBranch] = useState("main")
  const [uploadedFiles, setUploadedFiles] = useState<FileList | null>(null)
  const [error, setError] = useState<string | null>(null)

  const mutation = useMutation({
    mutationFn: async () => {
      if (sourceType === "local_folder") {
        if (!uploadedFiles || uploadedFiles.length === 0) {
          throw new Error("Select a project folder to upload")
        }

        const formData = new FormData()
        formData.append("commit_sha", commitSha || "HEAD")
        formData.append("default_branch", defaultBranch)
        formData.append("source_type", "local_folder")

        // 1. Dossiers à exclure (inclut les variants de venv + dossiers internes venv)
        const SKIP_DIRS = new Set([
          ".venv", "venv", "env", ".env", "virtualenv", ".virtualenvs", "envs",
          "node_modules", ".git", "__pycache__", ".pytest_cache",
          "dist", "build", ".mypy_cache", ".ruff_cache", "coverage", ".tox",
          ".next", ".nuxt", ".output", ".vercel", ".netlify", "out",
          ".idea", ".vscode", ".vs", ".github", ".gitlab",
          ".cache", "tmp", "temp", "logs", "log",
          ".venv_old_broken", ".venv_old", "venv_old", "venv_bak", "env_bak",
          // Dossiers internes d'un venv Python (souvent en majuscule sur Windows)
          "lib", "lib64", "include", "scripts", "bin", "share", "man",
          "site-packages", "dist-packages", "libs",
        ])

        // 2. Extensions à garder (whitelist)
        const KEEP_EXTS = new Set([
          ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
          ".yml", ".yaml", ".json", ".tf", ".hcl", ".tpl",
          ".md", ".txt", ".toml", ".ini", ".cfg", ".conf",
          ".html", ".htm", ".css", ".scss", ".sass", ".less", ".styl",
          ".j2", ".jinja", ".jinja2",
          ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd",
          ".sql", ".graphql", ".proto", ".thrift",
          ".rs", ".go", ".java", ".kt", ".scala", ".rb", ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".swift", ".r",
          ".dockerfile"
        ])

        // 3. Fichiers sans extension à garder
        const KEEP_NAMES = new Set([
          "dockerfile", "dockerfile.dev", "dockerfile.prod", "dockerfile.test",
          "makefile", "makefile.win", "gnumakefile",
          "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
          "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
          "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock", "pipfile", "pipfile.lock",
          "tsconfig.json", "jsconfig.json",
          "vite.config.ts", "vite.config.js", "vite.config.mjs",
          "tailwind.config.js", "tailwind.config.ts",
          "next.config.js", "next.config.ts",
          "jest.config.js", "jest.config.ts",
          "babel.config.js", ".babelrc", ".babelrc.json",
          ".eslintrc", ".eslintrc.json", ".eslintrc.js", ".eslintrc.yaml",
          ".prettierrc", ".prettierrc.json", ".prettierrc.js",
          ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore", ".npmignore",
          "manifest.json", "robots.txt", "sitemap.xml",
          ".env", ".env.example", ".env.local", ".env.development", ".env.production",
          "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
          "nginx.conf", "apache.conf", "httpd.conf"
        ])

        let fileCount = 0
        const dirCounts: Record<string, number> = {}
        const passedFiles: string[] = []

        Array.from(uploadedFiles).forEach((file) => {
          const relPath = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
          const parts = relPath.split(/[/\\]/)

          // Skip excluded directories
          if (parts.some(part => SKIP_DIRS.has(part.toLowerCase()))) return

          const nameLower = file.name.toLowerCase()
          const ext = nameLower.includes(".") ? nameLower.slice(nameLower.lastIndexOf(".")) : ""

          // Keep by extension
          if (KEEP_EXTS.has(ext)) {
            formData.append("files", file, relPath)
            fileCount++
            const topDir = parts[0] || "root"
            dirCounts[topDir] = (dirCounts[topDir] || 0) + 1
            if (passedFiles.length < 20) passedFiles.push(relPath)
            return
          }

          // Keep by exact filename
          if (KEEP_NAMES.has(nameLower)) {
            formData.append("files", file, relPath)
            fileCount++
            const topDir = parts[0] || "root"
            dirCounts[topDir] = (dirCounts[topDir] || 0) + 1
            if (passedFiles.length < 20) passedFiles.push(relPath)
            return
          }

          // Skip everything else
        })

        console.log("=== UPLOAD DIAGNOSTIC ===")
        console.log("Files per top directory:", dirCounts)
        console.log("First 20 files passing filter:", passedFiles)
        console.log("=========================")

        alert(`Uploading ${fileCount} files (skipped ${uploadedFiles.length - fileCount})\n\nTop dirs: ${JSON.stringify(dirCounts, null, 2).slice(0, 500)}`)
        if (fileCount === 0) {
          throw new Error("No valid files to upload after filtering.")
        }
        if (fileCount > 2000) {
          throw new Error(`Too many files (${fileCount}). Check console (F12) for diagnostic.`)
        }

        return jobsApi.upload(formData)
      }

      const validUrl = getRepoUrl(sourceType, repoUrl)
      return jobsApi.create({
        repo_url: validUrl,
        source_type: sourceType,
        commit_sha: commitSha || "HEAD",
        default_branch: defaultBranch,
      })
    },
    onSuccess: (res) => {
      if (res.job_id) navigate(`/runs/${res.job_id}`)
    },
  })

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)

    if (sourceType === "local_folder") {
      if (!uploadedFiles || uploadedFiles.length === 0) {
        setError("Please upload a project folder first.")
        return
      }
      mutation.mutate()
      return
    }

    if (!isValidRepoUrl(repoUrl, sourceType)) {
      setError(
        sourceType === "gitlab"
          ? "Enter a valid GitLab repository URL, e.g. https://gitlab.com/org/repo"
          : "Enter a valid GitHub repository URL, e.g. https://github.com/org/repo",
      )
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
        <Card title="Repository" description="Choose a public repo or upload a local project folder.">
          <div className="space-y-4">
            <Field label="Repository source">
              <div className="grid grid-cols-3 gap-2">
                {[
                  { value: "github", label: "GitHub" },
                  { value: "gitlab", label: "GitLab" },
                  { value: "local_folder", label: "Local folder" },
                ].map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    onClick={() => setSourceType(option.value as SourceType)}
                    className={`rounded-md border px-3 py-2 text-sm transition ${
                      sourceType === option.value
                        ? "border-accent bg-accent/10 text-foreground"
                        : "border-border bg-surface-2 text-muted hover:border-border-strong"
                    }`}
                  >
                    {option.label}
                  </button>
                ))}
              </div>
            </Field>

            {sourceType !== "local_folder" && (
              <Field label={sourceType === "gitlab" ? "GitLab URL" : "GitHub URL"} hint="Public repository">
                <div className="relative">
                  <span className="pointer-events-none absolute inset-y-0 left-0 flex items-center pl-3 text-faint">
                    <IconRepo className="size-4" />
                  </span>
                  <Input
                    autoFocus
                    value={repoUrl}
                    onChange={(e) => setRepoUrl(e.target.value)}
                    placeholder={
                      sourceType === "gitlab"
                        ? "https://gitlab.com/org/project"
                        : "https://github.com/org/repo"
                    }
                    className="pl-9 font-mono text-[13px]"
                  />
                </div>
              </Field>
            )}

            {sourceType === "local_folder" && (
              <Field label="Project folder" hint="Upload a directory">
                <input
                  {...({
                    type: "file",
                    multiple: true,
                    webkitdirectory: "",
                    directory: "",
                    onChange: (e: ChangeEvent<HTMLInputElement>) => setUploadedFiles(e.target.files),
                    className:
                      "block w-full rounded-md border border-border bg-surface-2 px-3 py-2 text-sm text-foreground file:mr-3 file:rounded file:border-0 file:bg-accent/10 file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-accent",
                  } as any)}
                />
              </Field>
            )}

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

function getRepoUrl(_sourceType: SourceType, value: string): string {
  return value.trim().replace(/\/+$/, "")
}

function isValidRepoUrl(url: string, sourceType: SourceType): boolean {
  try {
    const u = new URL(url)
    if (u.protocol !== "https:") return false
    const parts = u.pathname.split("/").filter(Boolean)
    if (sourceType === "github") {
      return u.hostname.endsWith("github.com") && parts.length >= 2
    }
    if (sourceType === "gitlab") {
      return u.hostname.endsWith("gitlab.com") && parts.length >= 2
    }
    return false
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