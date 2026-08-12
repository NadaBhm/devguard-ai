import { useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { jobsApi } from "../../api/jobs"
import { Button } from "../ui/Button"
import { Textarea } from "../ui/Inputs"
import { Badge } from "../ui/Badge"
import { IconAlert, IconRefresh } from "../icons"
import { formatCurrency } from "../../lib/format"
import { useToast } from "../ui/useToast"
import type { GateContext } from "../../types/jobs"

function GateDetails({ context }: { context?: GateContext }) {
  if (!context) return null
  const rows: Array<[string, string]> = []
  if (context.security_score != null) rows.push(["Security score", `${context.security_score}/100`])
  if (context.grade) rows.push(["Grade", context.grade])
  if (context.vulnerabilities_count != null) rows.push(["Vulnerabilities", String(context.vulnerabilities_count)])
  if (context.secrets_count != null) rows.push(["Secrets", String(context.secrets_count)])
  if (context.monthly_cost_usd != null) rows.push(["Est. monthly cost", formatCurrency(context.monthly_cost_usd)])
  if (context.architecture) rows.push(["Architecture", context.architecture])

  if (rows.length === 0) return null
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-[12.5px]">
      {rows.map(([k, v]) => (
        <div key={k} className="flex justify-between gap-3 py-0.5">
          <dt className="text-faint">{k}</dt>
          <dd className="font-medium text-foreground">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

export function GateApproval({
  jobId,
  gateName,
  message,
  context,
  onDecided,
}: {
  jobId: string
  gateName: string
  message?: string
  context?: GateContext
  onDecided?: () => void
}) {
  const [comment, setComment] = useState("")
  const [decision, setDecision] = useState<"approve" | "reject" | "regenerate" | null>(null)
  const queryClient = useQueryClient()
  const { showToast } = useToast()

  const mutation = useMutation({
    mutationFn: (args: { approved: boolean; requestRegeneration?: boolean }) =>
      jobsApi.approve(jobId, {
        approved: args.approved,
        comment,
        approved_by: "frontend-user@example.com",
        request_regeneration: args.requestRegeneration,
      }),
    onSuccess: (_data, variables) => {
      if (variables.requestRegeneration) {
        showToast({
          title: "Artifacts updated — review and re-approve",
          description:
            "The infrastructure artifacts were regenerated from your feedback. The run is paused for a new decision.",
        })
      }
      setDecision(null)
      setComment("")
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] })
      void queryClient.invalidateQueries({ queryKey: ["job-results", jobId] })
      onDecided?.()
    },
  })

  const isGate2 = gateName === "gate_2_pre_deployops"
  const iteration = context?.iteration ?? 0
  const maxIterations = context?.max_iterations ?? 3
  const canRegenerate = isGate2 && iteration < maxIterations

  return (
    <div className="rounded-lg border border-high/30 bg-high/[0.07]">
      <div className="flex items-center justify-between gap-3 border-b border-high/20 px-4 py-3">
        <div className="flex items-center gap-2.5">
          <span className="flex size-6 items-center justify-center rounded-full border border-high/40 text-high">
            <IconAlert className="size-3.5" />
          </span>
          <div>
            <p className="text-[13px] font-medium text-foreground">
              Approval required · <span className="font-mono text-[12px] text-high">{gateName}</span>
            </p>
            <p className="text-[12px] text-muted">
              {message ?? "A decision is required to continue the pipeline."}
            </p>
          </div>
        </div>
        <Badge tone="warning" dot>
          Waiting
        </Badge>
      </div>

      <div className="px-4 py-3">
        <GateDetails context={context} />
      </div>

      <div className="flex flex-col gap-3 px-4 py-3.5">
        <div>
          <Textarea
            aria-label="Decision comment"
            placeholder={
              isGate2
                ? "Approve, reject, or describe changes to regenerate the infrastructure artifacts (e.g. \"make it cheaper\", \"use 2 AZs\")"
                : "Add a comment for the record (optional)"
            }
            value={comment}
            onChange={(e) => setComment(e.target.value)}
            className="min-h-[64px]"
          />
          {isGate2 && (
            <p className="mt-1 text-[11.5px] text-faint">
              {canRegenerate
                ? `Regeneration remaining: ${maxIterations - iteration} of ${maxIterations}. Your comment becomes the prompt sent to the LLM.`
                : "Regeneration limit reached — choose approve or reject."}
            </p>
          )}
        </div>
        <div className="flex flex-wrap justify-end gap-2">
          <Button
            variant="ghost"
            onClick={() => {
              setDecision("reject")
              mutation.mutate({ approved: false })
            }}
            loading={mutation.isPending && decision === "reject"}
            disabled={decision !== null && decision !== "reject"}
            className="text-critical hover:bg-critical/10 hover:text-critical"
          >
            Reject
          </Button>
          {canRegenerate && (
            <Button
              variant="ghost"
              icon={<IconRefresh className="size-3.5" />}
              onClick={() => {
                setDecision("regenerate")
                mutation.mutate({ approved: false, requestRegeneration: true })
              }}
              loading={mutation.isPending && decision === "regenerate"}
              disabled={!comment.trim() || (decision !== null && decision !== "regenerate")}
              className="text-accent hover:bg-accent/10 hover:text-accent"
            >
              Regenerate with feedback
            </Button>
          )}
          <Button
            variant="primary"
            onClick={() => {
              setDecision("approve")
              mutation.mutate({ approved: true })
            }}
            loading={mutation.isPending && decision === "approve"}
            disabled={decision !== null && decision !== "approve"}
          >
            Approve & continue
          </Button>
        </div>
        {mutation.isError && (
          <p className="text-[12px] text-critical">
            {mutation.error instanceof Error ? mutation.error.message : "Approval failed."}
          </p>
        )}
      </div>
    </div>
  )
}