import { useMemo, useState } from "react"
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { jobsApi } from "../../api/jobs"
import type { TerraformArtifact } from "../../types/results"
import { IconDownload, IconFile } from "../icons"
import { Badge } from "../ui/Badge"
import { Button } from "../ui/Button"
import { useToast } from "../ui/useToast"

function downloadFile(artifact: TerraformArtifact) {
  const blob = new Blob([artifact.content], { type: "text/plain;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = artifact.file_path.split("/").pop() ?? "artifact.txt"
  a.click()
  URL.revokeObjectURL(url)
}

function downloadAll(artifacts: TerraformArtifact[]) {
  const content = artifacts.map((a) => `# ${a.file_path}\n${a.content}`).join("\n\n")
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = "devguard-artifacts.txt"
  a.click()
  URL.revokeObjectURL(url)
}

const ARTIFACT_BADGE_TONE: Record<string, "accent" | "neutral" | "info"> = {
  terraform: "accent",
  dockerfile: "info",
  "docker-image": "neutral",
}

function ArtifactTypeBadge({ artifactType }: { artifactType: string }) {
  const tone = ARTIFACT_BADGE_TONE[artifactType] ?? "neutral"
  return <Badge tone={tone}>{artifactType}</Badge>
}

export function TerraformTab({
  artifacts,
  editable = false,
}: {
  artifacts: TerraformArtifact[]
  editable?: boolean
}) {
  // Select by file_path, not id: the derived artifact rows get a fresh uuid on
  // every results refetch, so an id-based selection would go stale and snap
  // back to the first artifact. file_path is stable across refetches.
  const [selected, setSelected] = useState<string | null>(null)
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState("")
  const queryClient = useQueryClient()
  const { showToast } = useToast()

  const active = useMemo(
    () => artifacts.find((a) => a.file_path === selected) ?? artifacts[0],
    [artifacts, selected],
  )

  const saveMutation = useMutation({
    mutationFn: (content: string) => jobsApi.editArtifacts(active.run_id, [{ file_path: active.file_path, content }]),
    onSuccess: () => {
      setEditing(null)
      setDraft("")
      showToast({
        title: "Artifacts saved",
        description: `${active.file_path} updated and validated.`,
      })
      void queryClient.invalidateQueries({ queryKey: ["job-results", active.run_id] })
      void queryClient.invalidateQueries({ queryKey: ["job", active.run_id] })
    },
    onError: (error) => {
      showToast({
        title: "Save failed",
        description: error instanceof Error ? error.message : "The artifact could not be saved.",
        tone: "error",
      })
    },
  })

  if (artifacts.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <p className="max-w-sm text-[13px] text-muted">No generated artifacts recorded for this run yet.</p>
      </div>
    )
  }

  const startEditing = () => {
    if (!active) return
    setEditing(active.file_path)
    setDraft(active.content)
  }

  const cancelEditing = () => {
    setEditing(null)
    setDraft("")
  }

  const isEditing = editing === active?.file_path

  return (
    <div className="flex">
      <div className="w-60 shrink-0 border-r border-border">
        <div className="flex items-center justify-between px-3 py-2">
          <p className="text-[11px] font-medium uppercase tracking-wider text-faint">Artifacts</p>
          <button
            type="button"
            onClick={() => downloadAll(artifacts)}
            className="text-[11px] text-accent hover:text-accent-strong"
            title="Download all"
          >
            Download all
          </button>
        </div>
        <ul>
          {artifacts.map((a) => (
            <li key={a.id}>
              <button
                type="button"
                onClick={() => {
                  setSelected(a.file_path)
                  cancelEditing()
                }}
                className={`flex w-full items-center gap-2 border-l-2 px-3 py-2 text-left font-mono text-[12px] transition-colors ${
                  active?.file_path === a.file_path
                    ? "border-accent bg-surface-2 text-foreground"
                    : "border-transparent text-muted hover:bg-surface-2/60 hover:text-foreground"
                }`}
              >
                <IconFile className="size-3.5 shrink-0 text-faint" />
                <span className="truncate">{a.file_path}</span>
                {a.edited_at && <span className="ml-auto size-1.5 shrink-0 rounded-full bg-accent" title="Edited" />}
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex-1">
        {active && (
          <div className="flex flex-col">
            <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2">
              <div className="flex min-w-0 items-center gap-2">
                <code className="truncate font-mono text-[12px] text-muted">{active.file_path}</code>
                <ArtifactTypeBadge artifactType={active.artifact_type} />
                {active.edited_at && (
                  <Badge tone="warning" dot>
                    Edited{active.edited_by ? ` by ${active.edited_by}` : ""}
                  </Badge>
                )}
              </div>
              {editable && !isEditing && (
                <div className="flex shrink-0 items-center gap-2">
                  <Button
                    size="sm"
                    variant="secondary"
                    icon={<IconDownload className="size-3.5" />}
                    onClick={() => downloadFile(active)}
                  >
                    Download
                  </Button>
                  <Button size="sm" variant="primary" onClick={startEditing}>
                    Edit
                  </Button>
                </div>
              )}
              {editable && isEditing && (
                <div className="flex shrink-0 items-center gap-2">
                  <Button size="sm" variant="ghost" onClick={cancelEditing}>
                    Cancel
                  </Button>
                  <Button
                    size="sm"
                    variant="primary"
                    loading={saveMutation.isPending}
                    disabled={draft === active.content}
                    onClick={() => saveMutation.mutate(draft)}
                  >
                    Save
                  </Button>
                </div>
              )}
            </div>
            {editable && isEditing ? (
              <textarea
                aria-label={`Edit ${active.file_path}`}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
                className="max-h-[480px] min-h-[320px] w-full resize-y bg-surface p-4 font-mono text-[12.5px] leading-relaxed text-foreground outline-none focus:ring-2 focus:ring-accent/40"
              />
            ) : (
              <pre className="max-h-[480px] overflow-auto p-4 font-mono text-[12.5px] leading-relaxed text-foreground">
                <code>{active.content}</code>
              </pre>
            )}
            {saveMutation.isError && (
              <div className="border-t border-border px-4 py-2 text-[12px] text-critical">
                {saveMutation.error instanceof Error ? saveMutation.error.message : "Save failed."}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}