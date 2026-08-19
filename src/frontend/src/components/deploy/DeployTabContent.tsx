import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { jobsApi } from "../../api/jobs"
import type { Deployment, DeploymentStatus } from "../../types/results"
import { formatCurrency, formatDate } from "../../lib/format"
import { Button } from "../ui/Button"
import { Badge } from "../ui/Badge"
import { IconCheck, IconExternal, IconX } from "../icons"

const statusMeta: Record<DeploymentStatus, { label: string; tone: "accent" | "neutral" | "success" | "warning" | "danger" }> = {
  pending: { label: "Pending", tone: "neutral" },
  applying: { label: "Applying", tone: "warning" },
  succeeded: { label: "Succeeded", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  rolled_back: { label: "Rolled back", tone: "danger" },
  destroyed: { label: "Destroyed", tone: "neutral" },
}

const envMeta = {
  dev: { label: "Development", dot: "bg-low" },
  staging: { label: "Staging", dot: "bg-medium" },
  prod: { label: "Production", dot: "bg-accent" },
}

function getTerraformOutputs(infrastructureJson: Record<string, unknown> | null): {
  ecsClusterName?: string;
  serviceName?: string;
  albDns?: string;
} {
  if (!infrastructureJson?.terraform_outputs) return {};
  const outputs = infrastructureJson.terraform_outputs as Record<string, unknown>;
  return {
    ecsClusterName: outputs.ecs_cluster_name as string,
    serviceName: outputs.service_name as string,
    albDns: outputs.alb_dns as string,
  };
}

function normalizeExternalUrl(value: string | null | undefined): string | undefined {
  if (!value) return undefined

  const trimmed = value.trim()
  if (!trimmed) return undefined
  if (/^https?:\/\//i.test(trimmed)) return trimmed
  if (trimmed.startsWith("//")) return `http:${trimmed}`
  if (/^[a-z0-9.-]+(?::\d+)?(?:\/.*)?$/i.test(trimmed) && !trimmed.startsWith("/")) {
    return `http://${trimmed}`
  }

  return undefined
}

function getDeployedUrl(infrastructureJson: Record<string, unknown> | null): string | undefined {
  if (!infrastructureJson) return undefined

  const deployedUrl = normalizeExternalUrl(infrastructureJson.deployed_url as string | undefined)
  if (deployedUrl) return deployedUrl

  const outputs = getTerraformOutputs(infrastructureJson)
  if (outputs.albDns) return normalizeExternalUrl(outputs.albDns)
  return undefined
}

export function DeployTab({ deployments, jobId }: { deployments: Deployment[]; jobId?: string }) {
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null)
  const [rollbackError, setRollbackError] = useState<string | null>(null)
  const [rollbackResult, setRollbackResult] = useState<{ message?: string; task_definition?: string } | null>(null)
  const queryClient = useQueryClient()

  const canRollback = !!jobId && deployments.some((d) => d.status === "succeeded")

  const revisionsQuery = useQuery({
    queryKey: ["deployment-revisions", jobId],
    queryFn: () => jobsApi.deploymentRevisions(jobId!),
    enabled: canRollback,
  })

  useEffect(() => {
    if (revisionsQuery.data?.versions?.length) {
      const current = revisionsQuery.data.versions.find((v) => v.is_current)
      if (current?.revision != null && selectedRevision == null) {
        setSelectedRevision(current.revision)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revisionsQuery.data])

  const versions = revisionsQuery.data?.versions ?? []
  const currentRevision = versions.find((v) => v.is_current)?.revision ?? null

  const destroyTargetDeployment =
    deployments.find((d) => d.status === "succeeded") ?? deployments.find((d) => d.status === "destroyed")
  const destroyExpectedServiceName = getTerraformOutputs(destroyTargetDeployment?.infrastructure_json ?? null).serviceName

  const rollback = useMutation({
    mutationFn: () =>
      jobsApi.rollback(jobId!, {
        reason: "Manual rollback requested from UI",
        target_revision: selectedRevision,
      }),
    onSuccess: (res) => {
      setRollbackResult({
        message: res.result?.message,
        task_definition: res.result?.task_definition,
      })
      setRollbackError(null)
      void queryClient.invalidateQueries({ queryKey: ["job-results", jobId] })
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] })
      void queryClient.invalidateQueries({ queryKey: ["deployment-revisions", jobId] })
    },
    onError: (err) => {
      setRollbackError(err instanceof Error ? err.message : "Rollback failed.")
    },
  })

  const [destroyConfirmText, setDestroyConfirmText] = useState("")
  const [destroyError, setDestroyError] = useState<string | null>(null)
  const [destroyResult, setDestroyResult] = useState<{ status: string; message?: string } | null>(null)

  const destroy = useMutation({
    mutationFn: (serviceName: string) =>
      jobsApi.destroy(jobId!, { confirm_service_name: serviceName }),
    onSuccess: (res) => {
      setDestroyResult({ status: res.result?.status, message: res.result?.message })
      setDestroyError(null)
      setDestroyConfirmText("")
      void queryClient.invalidateQueries({ queryKey: ["job-results", jobId] })
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] })
    },
    onError: (err) => {
      setDestroyError(err instanceof Error ? err.message : "Destroy failed.")
    },
  })

  if (deployments.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <p className="max-w-sm text-[13px] text-muted">No deployment records for this run yet.</p>
      </div>
    )
  }

  return (
    <div className="space-y-3 p-4">
      {canRollback && (
        <div className="space-y-3 rounded-lg border border-border bg-surface px-4 py-3">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="text-[13px] font-medium text-foreground">Deployment actions</p>
              <p className="text-[12px] text-muted">
                Choose a task-definition revision to roll back to, or use the latest previous one.
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            <div className="flex min-w-[220px] flex-1 items-center gap-2">
              <select
                aria-label="Target revision"
                value={selectedRevision ?? ""}
                onChange={(e) => setSelectedRevision(e.target.value ? Number(e.target.value) : null)}
                disabled={revisionsQuery.isLoading}
                className="h-8 flex-1 rounded-md border border-border bg-surface-2 px-2.5 text-[13px] text-foreground outline-none focus:border-accent disabled:opacity-45"
              >
                {revisionsQuery.isLoading && <option>Loading versionsÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬Ãƒâ€šÃ‚Â¦</option>}
                {!revisionsQuery.isLoading && versions.length === 0 && (
                  <option value="">No versions available</option>
                )}
                {versions.map((v) => (
                  <option key={v.task_definition_arn} value={v.revision ?? ""}>
                    Revision {v.revision ?? "?"}{v.is_current ? " (current)" : ""}
                  </option>
                ))}
              </select>
            </div>

            <Button
              variant="danger"
              size="sm"
              onClick={() => rollback.mutate()}
              loading={rollback.isPending}
              disabled={rollback.isPending || selectedRevision == null || selectedRevision === currentRevision}
            >
              Rollback to selected
            </Button>
          </div>

          {rollbackResult && (
            <p className="text-[12px] text-accent">
              Rollback succeeded{rollbackResult.message ? ` ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â· ${rollbackResult.message}` : ""}
              {rollbackResult.task_definition ? ` (${rollbackResult.task_definition})` : ""}
            </p>
          )}
          {rollbackError && <p className="text-[12px] text-critical">{rollbackError}</p>}
          {revisionsQuery.isError && (
            <p className="text-[12px] text-critical">
              {revisionsQuery.error instanceof Error ? revisionsQuery.error.message : "Failed to load versions."}
            </p>
          )}
        </div>
      )}

      {jobId && destroyTargetDeployment && (
        <div className="space-y-3 rounded-lg border border-critical/30 bg-surface px-4 py-3">
          <div>
            <p className="text-[13px] font-medium text-foreground">Danger zone</p>
            <p className="text-[12px] text-muted">
              Permanently destroy the deployed infrastructure for this job. This cannot be undone.
            </p>
            {destroyExpectedServiceName && (
              <p className="text-[12px] text-muted">
                Type <span className="font-mono text-foreground">{destroyExpectedServiceName}</span> to confirm.
              </p>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <input
              type="text"
              aria-label="Type the service name to confirm"
              placeholder={destroyExpectedServiceName ?? "Type the service name to confirm"}
              value={destroyConfirmText}
              onChange={(e) => setDestroyConfirmText(e.target.value)}
              disabled={destroy.isPending}
              className="h-8 min-w-[220px] flex-1 rounded-md border border-border bg-surface-2 px-2.5 text-[13px] text-foreground outline-none focus:border-accent disabled:opacity-45"
            />
            <Button
              variant="danger"
              size="sm"
              onClick={() => destroy.mutate(destroyConfirmText)}
              loading={destroy.isPending}
              disabled={
                destroy.isPending ||
                destroyConfirmText.trim().length === 0 ||
                (!!destroyExpectedServiceName && destroyConfirmText !== destroyExpectedServiceName)
              }
            >
              Destroy deployment
            </Button>
          </div>

          {destroyResult && (
            <p className={destroyResult.status === "success" ? "text-[12px] text-accent" : "text-[12px] text-warning"}>
              {destroyResult.status === "success"
                ? "Deployment destroyed successfully."
                : destroyResult.message ?? `Status: ${destroyResult.status}`}
            </p>
          )}
          {destroyError && <p className="text-[12px] text-critical">{destroyError}</p>}
        </div>
      )}

      {deployments.map((d) => {
        const meta = statusMeta[d.status]
        const envMetaFor = envMeta[d.environment] ?? envMeta.dev
        const tfOutputs = getTerraformOutputs(d.infrastructure_json)
        const deployedUrl = getDeployedUrl(d.infrastructure_json)
        return (
          <div key={d.id} className="rounded-lg border border-border bg-surface-2">
            <div className="flex items-center gap-3 px-4 py-3">
              <span className={`size-2 rounded-full ${envMetaFor.dot}`} />
              <div className="min-w-0 flex-1">
                <p className="text-[13px] font-medium text-foreground">{envMetaFor.label}</p>
                <p className="font-mono text-[11.5px] text-faint">{d.aws_region}</p>
              </div>
              <Badge tone={meta.tone}>{meta.label}</Badge>
              {d.status === "succeeded" && <IconCheck className="size-4 text-accent" />}
              {d.status === "failed" && <IconX className="size-4 text-critical" />}
            </div>
            <dl className="grid grid-cols-2 border-t border-border px-4 py-2.5 text-[12.5px] gap-x-6 gap-y-1.5">
              <div className="flex justify-between">
                <dt className="text-faint">Applied</dt>
                <dd className="font-medium text-foreground">{d.applied_at ? formatDate(d.applied_at) : "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-faint">Cost / mo</dt>
                <dd className="font-medium tabular text-foreground">{d.cost_total_monthly != null ? formatCurrency(d.cost_total_monthly) : "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-faint">Terraform</dt>
                <dd className="font-mono text-muted">{d.terraform_version ?? "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â"}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-faint">State ID</dt>
                <dd className="max-w-[180px] truncate font-mono text-muted">{d.terraform_state_id ?? "ÃƒÆ’Ã‚Â¢ÃƒÂ¢Ã¢â‚¬Å¡Ã‚Â¬ÃƒÂ¢Ã¢â€šÂ¬Ã‚Â"}</dd>
              </div>
              {tfOutputs.ecsClusterName && (
                <div className="col-span-2 flex justify-between">
                  <dt className="text-faint">ECS Cluster</dt>
                  <dd className="font-mono text-foreground">{tfOutputs.ecsClusterName}</dd>
                </div>
              )}
              {tfOutputs.serviceName && (
                <div className="col-span-2 flex justify-between">
                  <dt className="text-faint">ECS Service</dt>
                  <dd className="font-mono text-foreground">{tfOutputs.serviceName}</dd>
                </div>
              )}
              {deployedUrl && (
                <div className="col-span-2 flex justify-between items-center">
                  <dt className="text-faint">URL</dt>
                  <dd className="flex items-center gap-2">
                    <a
                      href={deployedUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-mono text-accent hover:underline flex items-center gap-1.5"
                    >
                      {deployedUrl}
                      <IconExternal className="size-3.5" />
                    </a>
                  </dd>
                </div>
              )}
              {d.rollback_reason && (
                <div className="col-span-2 flex justify-between">
                  <dt className="text-faint">Rollback reason</dt>
                  <dd className="text-critical">{d.rollback_reason}</dd>
                </div>
              )}
            </dl>
          </div>
        )
      })}
    </div>
  )
}