import { client } from "./client"
import type { User, UserRole } from "../types/auth"

export const adminApi = {
  async listUsers(): Promise<User[]> {
    return client.get<User[]>("/admin/users")
  },
  async updateRole(userId: string, role: UserRole): Promise<User> {
    return client.put<User>(`/admin/users/${userId}/role?role=${role}`)
  },
}
