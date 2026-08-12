import { client } from "./client"

export interface DeploymentItem {
  id: string
  run_id: string
  project_id: string
  repo_name: string | null
  repo_url: string | null
  environment: "dev" | "staging" | "prod"
  aws_region: string
  terraform_version: string | null
  terraform_state_id: string | null
  status: string
  applied_at: string | null
  created_at: string
  rollback_reason: string | null
  cost_total_monthly: number | null
}

export interface DeploymentsListResponse {
  deployments: DeploymentItem[]
}

export const deploymentsApi = {
  async list(): Promise<DeploymentItem[]> {
    const res = await client.get<DeploymentsListResponse>("/deployments/")
    return res.deployments
  },
}
