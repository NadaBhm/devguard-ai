import { useMemo, useState } from "react"
import type { TerraformArtifact } from "../../types/results"
import { IconCheck, IconCopy, IconDownload, IconFile } from "../icons"
import { Button } from "../ui/Button"
import { HclViewer } from "./HclViewer"

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

export function TerraformTab({ artifacts }: { artifacts: TerraformArtifact[] }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const active = useMemo(
    () => artifacts.find((a) => a.id === selected) ?? artifacts[0],
    [artifacts, selected],
  )

  async function copyContent(artifact: TerraformArtifact) {
    await navigator.clipboard.writeText(artifact.content)
    setCopiedId(artifact.id)
    setTimeout(() => setCopiedId((current) => (current === artifact.id ? null : current)), 1500)
  }

  if (artifacts.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-16 text-center">
        <p className="max-w-sm text-[13px] text-muted">No generated artifacts recorded for this run yet.</p>
      </div>
    )
  }

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
                onClick={() => setSelected(a.id)}
                className={`flex w-full items-center gap-2 border-l-2 px-3 py-2 text-left font-mono text-[12px] transition-colors ${
                  active?.id === a.id
                    ? "border-accent bg-surface-2 text-foreground"
                    : "border-transparent text-muted hover:bg-surface-2/60 hover:text-foreground"
                }`}
              >
                <IconFile className="size-3.5 shrink-0 text-faint" />
                <span className="truncate">{a.file_path}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="flex-1">
        {active && (
          <div className="flex flex-col">
            <div className="flex items-center justify-between border-b border-border px-4 py-2">
              <code className="font-mono text-[12px] text-muted">{active.file_path}</code>
              <div className="flex items-center gap-2">
                <Button
                  size="sm"
                  variant="secondary"
                  icon={
                    copiedId === active.id ? (
                      <IconCheck className="size-3.5 text-accent" />
                    ) : (
                      <IconCopy className="size-3.5" />
                    )
                  }
                  onClick={() => copyContent(active)}
                >
                  {copiedId === active.id ? "Copied" : "Copy"}
                </Button>
                <Button size="sm" variant="secondary" icon={<IconDownload className="size-3.5" />} onClick={() => downloadFile(active)}>
                  Download
                </Button>
              </div>
            </div>
            <HclViewer code={active.content} />
          </div>
        )}
      </div>
    </div>
  )
}