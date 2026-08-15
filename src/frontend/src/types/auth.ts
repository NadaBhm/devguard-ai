export type UserRole = "member" | "admin" | "owner"

export interface User {
  id: string
  email: string
  username?: string | null
  first_name: string
  last_name: string
  role: UserRole
  is_verified: boolean
  created_at: string
  updated_at: string
  deleted_at?: string | null
}

export interface UserStats {
  total_projects: number
  total_runs: number
  total_findings: number
  total_deployments: number
  est_monthly_cost: number
  member_since: string
}

export interface LoginCredentials {
  email: string
  password: string
}

export interface RegisterInput extends LoginCredentials {
  first_name: string
  last_name: string
}

export interface UserUpdate {
  email?: string
  password?: string
  first_name?: string
  last_name?: string
  username?: string
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
}