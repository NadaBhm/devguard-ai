export type UserRole = "member" | "admin" | "owner"

export interface User {
  id: string
  email: string
  first_name: string
  last_name: string
  role: UserRole
  is_verified: boolean
  created_at: string
  updated_at: string
  deleted_at?: string | null
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
}

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
}