import { STUB, mockList } from "./stub"
import type { Notification } from "../types/notification"

const SAMPLE: Notification[] = []

export const notificationsApi = {
  STUB,
  async list(): Promise<Notification[]> {
    return mockList(SAMPLE)
  },
  async markRead(_id: string): Promise<Notification> {
    throw new Error("notifications.markRead: not implemented (backend pending)")
  },
  async markAllRead(): Promise<void> {
    throw new Error("notifications.markAllRead: not implemented (backend pending)")
  },
}