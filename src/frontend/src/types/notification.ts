export type NotificationType = "finding" | "deployment" | "cost_alert" | "security_breach"
export type NotificationSeverity = "info" | "warning" | "critical"

export interface Notification {
  id: string
  user_id: string
  run_id: string
  type: NotificationType
  severity: NotificationSeverity
  title: string
  body: string
  related_finding_id: string | null
  is_read: boolean
  created_at: string
  read_at: string | null
  dismissed_at: string | null
}