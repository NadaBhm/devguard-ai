import { createContext } from "react"
import type { LoginCredentials, RegisterInput, User } from "../../types/auth"

export interface AuthContextValue {
  user: User | null
  initializing: boolean
  isAuthenticated: boolean
  login: (credentials: LoginCredentials) => Promise<void>
  register: (input: RegisterInput) => Promise<void>
  logout: () => void
  refreshUser: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)