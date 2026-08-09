import { client } from "./client"
import type { CostAlert } from "../types/alert"

interface AlertListResponse {
  alerts: CostAlert[]
}

export const alertsApi = {
  async list(resolved?: boolean): Promise<CostAlert[]> {
    const qs = resolved === undefined ? "" : `?resolved=${resolved}`
    const res = await client.get<AlertListResponse>(`/alerts/${qs}`)
    return res.alerts
  },
  async resolve(id: string): Promise<CostAlert> {
    return client.put<CostAlert>(`/alerts/${id}/resolve`)
  },
}
