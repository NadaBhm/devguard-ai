import { Link, useSearchParams } from "react-router-dom"

export type RunTab = "overview" | "codesec" | "infracost" | "terraform" | "deploy"

const TABS: Array<{ id: RunTab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "codesec", label: "CodeSec" },
  { id: "infracost", label: "InfraCost" },
  { id: "terraform", label: "Terraform" },
  { id: "deploy", label: "Deploy" },
]

export function RunTabs({ jobId, codesecCount }: { jobId: string; codesecCount?: number }) {
  const [params] = useSearchParams()
  const active = (params.get("tab") as RunTab | null) ?? "overview"

  return (
    <div className="flex items-center gap-1 overflow-x-auto border-b border-border">
      {TABS.map((tab) => (
        <Link
          key={tab.id}
          to={`/runs/${jobId}${tab.id === "overview" ? "" : `?tab=${tab.id}`}`}
          className={`relative -mb-px whitespace-nowrap border-b-2 px-3 py-2.5 text-[13px] transition-colors ${
            active === tab.id
              ? "border-accent font-medium text-foreground"
              : "border-transparent text-muted hover:text-foreground"
          }`}
        >
          {tab.label}
          {tab.id === "codesec" && codesecCount != null && codesecCount > 0 && (
            <span className="ml-1.5 rounded-full border border-border bg-surface-2 px-1.5 py-0.5 text-[10.5px] tabular text-muted">
              {codesecCount}
            </span>
          )}
        </Link>
      ))}
    </div>
  )
}