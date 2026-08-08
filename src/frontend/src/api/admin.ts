import { STUB, mockList } from "./stub"
import type { User, UserRole } from "../types/auth"

const SAMPLE: User[] = []

export const adminApi = {
  STUB,
  async listUsers(): Promise<User[]> {
    return mockList(SAMPLE)
  },
  async updateRole(_userId: string, _role: UserRole): Promise<User> {
    throw new Error("admin.updateRole: not implemented (backend pending)")
  },
}