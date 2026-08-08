const ACCESS_TOKEN = "devguard.access_token"
const REFRESH_TOKEN = "devguard.refresh_token"

export const tokenStore = {
  get access() {
    return localStorage.getItem(ACCESS_TOKEN)
  },
  get refresh() {
    return localStorage.getItem(REFRESH_TOKEN)
  },
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_TOKEN, access)
    localStorage.setItem(REFRESH_TOKEN, refresh)
  },
  clear() {
    localStorage.removeItem(ACCESS_TOKEN)
    localStorage.removeItem(REFRESH_TOKEN)
  },
}