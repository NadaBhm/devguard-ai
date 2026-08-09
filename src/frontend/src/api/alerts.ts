import { STUB, mockList } from "./stub"
import type { CostAlert } from "../types/alert"

const SAMPLE: CostAlert[] = []

export const alertsApi = {
  STUB,
  async list(): Promise<CostAlert[]> {
    return mockList(SAMPLE)
  },
  async resolve(_id: string): Promise<CostAlert> {
    throw new Error("alerts.resolve: not implemented (backend pending)")
  },
}