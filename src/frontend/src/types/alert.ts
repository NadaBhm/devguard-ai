export type CostAlertType = "budget_exceeded" | "cost_spike" | "unusual_resource"

export interface CostAlert {
  id: string
  run_id: string
  project_id: string
  user_id: string
  alert_type: CostAlertType
  threshold_usd: string | number
  actual_cost_usd: string | number
  severity: "warning" | "critical"
  is_resolved: boolean
  created_at: string
  resolved_at: string | null
}