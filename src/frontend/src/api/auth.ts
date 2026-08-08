import { client } from "./client"
import type { LoginCredentials, RegisterInput, TokenPair, User, UserUpdate } from "../types/auth"

export const authApi = {
  login: (credentials: LoginCredentials) => client.post<TokenPair>("/auth/login", credentials, false),
  register: (input: RegisterInput) => client.post<User>("/auth/register", input, false),
  refresh: (refresh_token: string) => client.post<TokenPair>("/auth/refresh", { refresh_token }, false),
  me: () => client.get<User>("/auth/me"),
  updateMe: (update: UserUpdate) => client.put<User>("/auth/me", update),
}