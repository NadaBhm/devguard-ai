import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react"
import { authApi } from "../../api/auth"
import { setUnauthorizedHandler } from "../../api/client"
import { tokenStore } from "../../api/tokenStore"
import { AuthContext, type AuthContextValue } from "./context"
import type { LoginCredentials, RegisterInput, User } from "../../types/auth"

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [initializing, setInitializing] = useState(true)

  const logout = useCallback(() => {
    tokenStore.clear()
    setUser(null)
  }, [])

  useEffect(() => {
    setUnauthorizedHandler(logout)
    return () => setUnauthorizedHandler(null)
  }, [logout])

  useEffect(() => {
    let cancelled = false
    async function bootstrap() {
      if (!tokenStore.access) {
        setInitializing(false)
        return
      }
      try {
        const me = await authApi.me()
        if (!cancelled) setUser(me)
      } catch {
        if (!cancelled) logout()
      } finally {
        if (!cancelled) setInitializing(false)
      }
    }
    void bootstrap()
    return () => {
      cancelled = true
    }
  }, [logout])

  const login = useCallback(async (credentials: LoginCredentials) => {
    const tokens = await authApi.login(credentials)
    tokenStore.set(tokens.access_token, tokens.refresh_token)
    const me = await authApi.me()
    setUser(me)
  }, [])

  const register = useCallback(async (input: RegisterInput) => {
    await authApi.register(input)
  }, [])

  const refreshUser = useCallback(async () => {
    const me = await authApi.me()
    setUser(me)
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      initializing,
      isAuthenticated: user !== null,
      login,
      register,
      logout,
      refreshUser,
    }),
    [user, initializing, login, register, logout, refreshUser],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}