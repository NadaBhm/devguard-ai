import { useEffect, useMemo } from "react"
import { useParams, useSearchParams } from "react-router-dom"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { jobsApi } from "../api/jobs"
import { useWebSocket } from "../hooks/useWebSocket"
import { Spinner } from "../components/ui/Button"
import { Card } from "../components/ui/Card"
import { RunStatusBadge } from "../components/jobs/RunStatusBadge"
import { RunTabs, type RunTab } from "../components/jobs/RunTabs"
import { ProgressTimeline } from "../components/jobs/ProgressTimeline"
import { GateApproval } from "../components/jobs/GateApproval"
import { CodeSecTab } from "../components/findings/CodeSecTabContent"
import { InfraCostTab } from "../components/cost/InfraCostTabContent"
import { TerraformTab } from "../components/terraform/TerraformTabContent"
import { DeployTab } from "../components/deploy/DeployTabContent"
import { formatCurrency, formatDate, formatDuration, shortId } from "../lib/format"
import type { GateContext, JobDetail, JobState, RunState } from "../types/jobs"

const VALID_TABS: RunTab[] = ["overview", "codesec", "infracost", "artifacts", "deploy"]

// Legacy tab aliases: pre-rename URLs used ?tab=terraform.
const LEGACY_TAB_ALIASES: Record<string, RunTab> = { terraform: "artifacts" }

interface InterruptInfo {
  gate: string
  message?: string
  context?: GateContext
}

function interruptInfo(state: JobState | undefined): InterruptInfo | null {
  if (!state?.__interrupt__ || state.__interrupt__.length === 0) return null
  const entry = state.__interrupt__[0]
  const value =
    entry != null && typeof entry === "object" && "value" in entry
      ? (entry as { value?: unknown }).value
      : entry
  if (!value || typeof value !== "object") return null
  const v = value as Record<string, unknown>
  if (typeof v.gate !== "string") return null
  return {
    gate: v.gate,
    message: typeof v.message === "string" ? v.message : undefined,
    context: (v.context as GateContext | undefined) ?? undefined,
  }
}

const GATE_ORDER: Array<{ name: string; text: string }> = [
  { name: "gate_1_pre_infracost", text: "CodeSec analysis complete. Approve to proceed with infrastructure generation?" },
  { name: "gate_2_pre_deployops", text: "InfraCost complete. Review the cost estimate and approve the deployment?" },
]

function pendingGate(state: JobState | undefined): InterruptInfo | null {
  const gates = state?.human_gates
  if (!gates) return null
  for (const { name, text } of GATE_ORDER) {
    const g = gates[name as keyof typeof gates]
    if (g && g.required && g.approved === null) {
      return { gate: name, message: text }
    }
  }
  return null
}

function repoFromState(state: JobState | undefined): string | undefined {
  return state?.repo_url ?? undefined
}

export function RunDetailPage() {
  const { jobId } = useParams<{ jobId: string }>()
  const [params] = useSearchParams()
  const rawTab = params.get("tab") as RunTab | null
  const mappedTab = rawTab ? (LEGACY_TAB_ALIASES[rawTab] ?? rawTab) : null
  const activeTab = mappedTab && VALID_TABS.includes(mappedTab) ? mappedTab : "overview"

  const jobQuery = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => jobsApi.get(jobId!),
    enabled: !!jobId,
    refetchInterval: 2500,
  })
  const resultsQuery = useQuery({
    queryKey: ["job-results", jobId],
    queryFn: () => jobsApi.results(jobId!),
    enabled: !!jobId,
    refetchInterval: 2500,
  })

  const ws = useWebSocket(jobId)
  const queryClient = useQueryClient()

  useEffect(() => {
    for (const e of ws.events) {
      if (e.type === "results_ready") {
        void queryClient.invalidateQueries({ queryKey: ["job-results", jobId] })
      } else if (e.type === "gate") {
        void queryClient.invalidateQueries({ queryKey: ["job", jobId] })
      }
    }
  }, [ws.events, jobId, queryClient])

  const job: JobDetail | undefined = jobQuery.data
  const results = resultsQuery.data

  const state: JobState | undefined =
    (job?.state as unknown as JobState | undefined) ?? undefined

  const orchestratorStatus: RunState | string | null | undefined =
    state?.status ?? job?.orchestrator_status

  const lastProgress = useMemo(
    () => ws.events.findLast((e) => e.type === "progress"),
    [ws.events],
  )

  const activePhase =
    lastProgress?.type === "progress" && ws.status === "open" ? lastProgress.phase : undefined

  const pending = useMemo(() => pendingGate(state), [state])
  const interrupt = useMemo(() => interruptInfo(state), [state])
  const gateName =
    interrupt?.gate ??
    pending?.gate ??
    (orchestratorStatus === "awaiting_approval_gate_1"
      ? "gate_1_pre_infracost"
      : orchestratorStatus === "awaiting_approval_gate_2"
        ? "gate_2_pre_deployops"
        : null)

  const gateInfo = interrupt ?? pending

  if (jobQuery.isLoading) {
    return (
      <div className="flex items-center justify-center gap-3 py-24 text-muted">
        <Spinner size={20} />
        <span className="text-sm">Loading run…</span>
      </div>
    )
  }
  if (jobQuery.isError || !jobId || !job) {
    return (
      <div className="py-24 text-center">
        <p className="text-sm text-faint">Unable to load this run.</p>
      </div>
    )
  }

  const repo = repoFromState(state)

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2.5">
            <h1 className="truncate font-mono text-lg font-semibold tracking-tight text-foreground">
              {repo ?? "Analysis run"}
            </h1>
            <RunStatusBadge status={job.status} orchestratorStatus={orchestratorStatus} />
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px] text-faint">
            <span>Started {formatDate(job.started_at)}</span>
            {job.duration_seconds != null && <span>Ran {formatDuration(job.duration_seconds)}</span>}
            <span className="font-mono">{shortId(jobId)}</span>
          </div>
        </div>
        {lastProgress?.type === "progress" && ws.status === "open" && (
          <span className="rounded-full border border-accent/25 bg-accent/10 px-3 py-1 text-[12px] text-accent">
            {lastProgress.message || "Running…"}
          </span>
        )}
      </header>

      {gateName && gateInfo ? (
        <GateApproval
          jobId={jobId}
          gateName={gateInfo.gate}
          message={gateInfo.message}
          context={gateInfo.context}
        />
      ) : null}

      <RunTabs jobId={jobId} codesecCount={results?.codesec_findings.length} />

      {activeTab === "overview" && (
        <div className="grid grid-cols-1 gap-5 lg:grid-cols-[280px_1fr]">
          <Card title="Pipeline">
            <ProgressTimeline
              status={orchestratorStatus as RunState}
              nodesExecuted={state?.orchestrator_metadata?.nodes_executed ?? []}
              activePhase={activePhase}
            />
          </Card>

          <div className="space-y-5">
            <Card title="Run">
              <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-[13px]">
                <Detail label="Status">
                  <RunStatusBadge status={job.status} orchestratorStatus={orchestratorStatus} />
                </Detail>
                {job.started_at && <Detail label="Started">{formatDate(job.started_at)}</Detail>}
                {job.completed_at && <Detail label="Finished">{formatDate(job.completed_at)}</Detail>}
                {job.duration_seconds != null && (
                  <Detail label="Duration">{formatDuration(job.duration_seconds)}</Detail>
                )}
                {repo && <Detail label="Repository">{repo}</Detail>}
              </dl>
            </Card>

            {results && (
              <Card title="Summary">
                <div className="grid grid-cols-3 gap-3">
                  <Metric label="Findings" value={String(results.codesec_findings.length)} />
                  <Metric
                    label="Cost / mo"
                    value={formatCurrency(
                      results.infracost_estimates.reduce(
                        (sum, e) => sum + Number(e.monthly_cost_usd || 0),
                        0,
                      ),
                    )}
                  />
                  <Metric label="Deployments" value={String(results.deployments.length)} />
                </div>
              </Card>
            )}

            {results?.agent_tasks.length ? (
              <Card title="Agents">
                <ul className="divide-y divide-border">
                  {results.agent_tasks.map((t) => (
                    <li key={t.id} className="flex items-center gap-3 py-2 text-[13px]">
                      <span className="font-mono text-foreground">{t.agent_name}</span>
                      <span className="text-faint">{t.status}</span>
                      {t.retry_count > 0 && (
                        <span className="ml-auto text-[12px] text-faint">retried {t.retry_count}×</span>
                      )}
                    </li>
                  ))}
                </ul>
              </Card>
            ) : null}

            {state?.error_log && state.error_log.length > 0 && (
              <Card title="Errors">
                <ul className="space-y-2">
                  {state.error_log.map((err, i) => (
                    <li key={i} className="flex items-start gap-2 text-[12.5px]">
                      <span className="mt-0.5 size-1.5 shrink-0 rounded-full bg-critical" />
                      <span className="text-muted">
                        <span className="font-mono text-critical">{err.node}</span> · {err.message}
                      </span>
                    </li>
                  ))}
                </ul>
              </Card>
            )}
          </div>
        </div>
      )}

      {activeTab === "codesec" && (
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <CodeSecTab findings={results?.codesec_findings ?? []} />
        </div>
      )}

      {activeTab === "infracost" && (
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <InfraCostTab
            estimates={results?.infracost_estimates ?? []}
            infracost={state?.infracost_result as JobState["infracost_result"]}
            iterations={state?.infracost_iterations}
          />
        </div>
      )}

      {activeTab === "artifacts" && (
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <TerraformTab
            artifacts={results?.terraform_artifacts ?? []}
            editable={gateName === "gate_2_pre_deployops"}
          />
        </div>
      )}

      {activeTab === "deploy" && (
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          <DeployTab deployments={results?.deployments ?? []} jobId={jobId} />
        </div>
      )}
    </div>
  )
}

function Detail({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-faint">{label}</dt>
      <dd className="mt-0.5 text-foreground">{children}</dd>
    </div>
  )
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface-2 px-3.5 py-3">
      <p className="text-[11px] font-medium uppercase tracking-wider text-faint">{label}</p>
      <p className="mt-1 text-lg font-semibold tabular tracking-tight text-foreground">{value}</p>
    </div>
  )
}