import { tokenStore } from "./tokenStore"

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = "ApiError"
    this.status = status
    this.detail = detail
  }
}

let refreshPromise: Promise<boolean> | null = null
let deauthHandler: (() => void) | null = null

export function setUnauthorizedHandler(handler: (() => void) | null) {
  deauthHandler = handler
}

async function refreshAccessToken(): Promise<boolean> {
  const refresh = tokenStore.refresh
  if (!refresh) return false

  const res = await fetch("/api/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: refresh }),
  })
  if (!res.ok) return false
  const data = (await res.json()) as { access_token: string; refresh_token: string }
  tokenStore.set(data.access_token, data.refresh_token)
  return true
}

async function request<T>(path: string, init: RequestInit = {}, auth = true): Promise<T> {
  const build = (token?: string) => ({
    ...init,
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(auth && token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  })

  const doFetch = () => fetch(`/api${path}`, build(tokenStore.access ?? undefined))

let res = await doFetch()
  if (res.status === 401 && auth) {
    refreshPromise ??= refreshAccessToken().finally(() => {
      refreshPromise = null
    })
    const ok = await refreshPromise
    if (ok) {
      res = await doFetch()
    } else {
      tokenStore.clear()
      deauthHandler?.()
    }
  }

  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = (await res.json()) as { detail?: string | unknown }
      if (typeof body.detail === "string") detail = body.detail
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const client = {
  get: <T>(path: string, auth = true) => request<T>(path, {}, auth),
  post: <T>(path: string, body?: unknown, auth = true) =>
    request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }, auth),
  put: <T>(path: string, body?: unknown, auth = true) =>
    request<T>(path, { method: "PUT", body: JSON.stringify(body) }, auth),
  patch: <T>(path: string, body?: unknown, auth = true) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }, auth),
}