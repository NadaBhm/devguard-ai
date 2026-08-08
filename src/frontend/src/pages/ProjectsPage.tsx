import { useQuery } from "@tanstack/react-query"
import { Link } from "react-router-dom"
import { projectsApi } from "../api/projects"
import { Button } from "../components/ui/Button"
import { Card } from "../components/ui/Card"
import { PageHeader } from "../components/ui/Misc"
import { EmptyState } from "../components/ui/Misc"
import { RunStatusBadge } from "../components/jobs/RunStatusBadge"
import { IconExternal, IconPlus, IconRepo } from "../components/icons"
import { formatDate, formatDuration } from "../lib/format"
import type { ProjectSummary } from "../api/projects"

function ProjectCard({ project }: { project: ProjectSummary }) {
  return (
    <div className="rounded-lg border border-border bg-surface transition-colors hover:border-border-strong">
      <div className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="flex items-center gap-2.5 min-w-0">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-md border border-border bg-surface-2 text-muted">
              <IconRepo className="size-4" />
            </span>
            <div className="min-w-0">
              <p className="truncate text-sm font-medium text-foreground">{project.repo_name}</p>
              <a
                href={project.repo_url}
                target="_blank"
                rel="noreferrer"
                className="flex items-center gap-1 truncate font-mono text-[11.5px] text-faint hover:text-accent"
              >
                {project.repo_url.replace(/^https?:\/\//, "")}
                <IconExternal className="size-3 shrink-0" />
              </a>
            </div>
          </div>
        </div>

        <dl className="mt-4 grid grid-cols-3 gap-x-3 gap-y-2 border-t border-border pt-3 text-[12px]">
          <div>
            <dt className="text-faint">Runs</dt>
            <dd className="mt-0.5 font-medium tabular text-foreground">{project.run_count}</dd>
          </div>
          <div>
            <dt className="text-faint">Last run</dt>
            <dd className="mt-0.5 font-medium text-foreground">
              {project.last_run ? formatDate(project.last_run.started_at) : "—"}
            </dd>
          </div>
          <div>
            <dt className="text-faint">Duration</dt>
            <dd className="mt-0.5 font-medium tabular text-foreground">
              {project.last_run ? formatDuration(project.last_run.duration_seconds) : "—"}
            </dd>
          </div>
        </dl>
      </div>

      <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-2.5 bg-surface-2/40">
        {project.last_run ? (
          <RunStatusBadge status={project.last_run.status} />
        ) : (
          <span className="text-[12px] text-faint">No runs yet</span>
        )}
        {project.last_run && (
          <span className="text-[12px] text-faint">{formatDuration(project.last_run.duration_seconds)}</span>
        )}
        {project.last_run && (
          <Link
            to={`/runs/${project.last_run.job_id}`}
            className="ml-auto text-[12px] text-accent hover:text-accent-strong"
          >
            Open run →
          </Link>
        )}
      </div>
    </div>
  )
}

export function ProjectsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["projects"],
    queryFn: projectsApi.list,
  })

  return (
    <div className="space-y-6">
      <PageHeader
        title="Projects"
        description="Repositories you've analyzed, derived from your run history."
        actions={
          <Button variant="primary" icon={<IconPlus className="size-4" />} to="/projects/new">
            New run
          </Button>
        }
      />

      {isLoading ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {[0, 1].map((i) => (
            <div key={i} className="h-44 animate-pulse rounded-lg bg-surface" />
          ))}
        </div>
      ) : isError ? (
        <Card>
          <p className="text-[13px] text-faint">Unable to load projects.</p>
        </Card>
      ) : data && data.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          {data.map((project) => (
            <ProjectCard key={project.repo_url} project={project} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<IconRepo className="size-5" />}
          title="No projects yet"
          description="Connect a repository by starting your first analysis run."
          action={
            <Button to="/projects/new" variant="primary">
              Analyze a repository
            </Button>
          }
        />
      )}
    </div>
  )
}