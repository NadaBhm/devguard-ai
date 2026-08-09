import { client } from "./client"
import type { Notification } from "../types/notification"

interface NotificationListResponse {
  notifications: Notification[]
}

export const notificationsApi = {
  async list(unreadOnly = false): Promise<Notification[]> {
    const qs = unreadOnly ? "?unread_only=true" : ""
    const res = await client.get<NotificationListResponse>(`/notifications/${qs}`)
    return res.notifications
  },
  async markRead(id: string): Promise<Notification> {
    return client.put<Notification>(`/notifications/${id}/read`)
  },
}
