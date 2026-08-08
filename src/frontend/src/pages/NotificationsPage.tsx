import { useQuery } from "@tanstack/react-query"
import { notificationsApi } from "../api/notifications"
import { Card } from "../components/ui/Card"
import { Badge } from "../components/ui/Badge"
import { EmptyState } from "../components/ui/Misc"
import { PageHeader } from "../components/ui/Misc"
import { IconBell } from "../components/icons"
import { formatDateLong } from "../lib/format"
import type { Notification, NotificationSeverity, NotificationType } from "../types/notification"

const typeMeta: Record<NotificationType, { label: string; tone: "accent" | "neutral" | "success" | "warning" | "danger" }> = {
  finding: { label: "Finding", tone: "danger" },
  deployment: { label: "Deployment", tone: "warning" },
  cost_alert: { label: "Cost", tone: "accent" },
  security_breach: { label: "Security", tone: "danger" },
}

function sevTone(sev: NotificationSeverity): "neutral" | "warning" | "danger" {
  if (sev === "critical") return "danger"
  if (sev === "warning") return "warning"
  return "neutral"
}

function Row({ n }: { n: Notification }) {
  const t = typeMeta[n.type]
  return (
    <li className="flex items-start gap-3 border-b border-border px-4 py-3 last:border-0">
      <span
        className={`mt-0.5 size-2 shrink-0 rounded-full ${
          n.is_read ? "bg-border-strong" : n.severity === "critical" ? "bg-critical" : "bg-accent"
        }`}
      />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <Badge tone={t.tone}>{t.label}</Badge>
          <Badge tone={sevTone(n.severity)}>{n.severity}</Badge>
          {!n.is_read && <span className="text-[11px] font-medium text-accent">New</span>}
        </div>
        <p className="mt-1 text-[13px] font-medium text-foreground">{n.title}</p>
        <p className="mt-0.5 text-[12.5px] leading-relaxed text-muted">{n.body}</p>
        <p className="mt-1.5 text-[11px] text-faint">{relativeTimeLong(n.created_at)}</p>
      </div>
    </li>
  )
}

export function NotificationsPage() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["notifications"],
    queryFn: notificationsApi.list,
  })

  const unread = (data ?? []).filter((n) => !n.is_read).length

  return (
    <div className="space-y-6">
      <PageHeader
        title="Activity"
        description="Findings, deployments, cost alerts and security events."
      />
      <Card
        title="Notifications"
        actions={
          data && data.length > 0 ? (
            <span className="text-[12px] tabular text-faint">
              {unread > 0 ? `${unread} unread` : "all read"}
            </span>
          ) : undefined
        }
        bodyClassName="p-0"
      >
        {isLoading ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2].map((i) => (
              <div key={i} className="h-14 animate-pulse rounded-md bg-surface-2" />
            ))}
          </div>
        ) : isError ? (
          <p className="p-4 text-[13px] text-faint">Unable to load notifications.</p>
        ) : data && data.length > 0 ? (
          <ul>
            {data.map((n) => (
              <Row key={n.id} n={n} />
            ))}
          </ul>
        ) : (
          <EmptyState
            icon={<IconBell className="size-5" />}
            title="No notifications yet"
            description="Findings, deployment status and cost alerts will appear here. The notifications API is not wired up yet."
          />
        )}
      </Card>
    </div>
  )
}

function relativeTimeLong(iso: string): string {
  const d = new Date(iso)
  const diff = Date.now() - d.getTime()
  const mins = Math.round(diff / 60000)
  if (mins < 1) return "just now"
  if (mins < 60) return `${mins} minute${mins === 1 ? "" : "s"} ago`
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? "" : "s"} ago`
  return formatDateLong(iso)
}