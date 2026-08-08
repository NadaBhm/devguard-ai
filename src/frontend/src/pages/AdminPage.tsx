import { useQuery } from "@tanstack/react-query"
import { useAuth } from "../features/auth/useAuth"
import { adminApi } from "../api/admin"
import { Card } from "../components/ui/Card"
import { EmptyState } from "../components/ui/Misc"
import { PageHeader } from "../components/ui/Misc"
import { Badge } from "../components/ui/Badge"
import { IconUsers } from "../components/icons"
import { formatDate } from "../lib/format"

export function AdminPage() {
  const { user } = useAuth()
  const { data, isLoading, isError } = useQuery({
    queryKey: ["admin-users"],
    queryFn: adminApi.listUsers,
  })

  const isAdmin = user?.role === "admin" || user?.role === "owner"

  return (
    <div className="space-y-6">
      <PageHeader title="Admin" description="User and role management." />

      {isAdmin ? (
        <Card title="Users" bodyClassName="p-0">
          {isLoading ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2].map((i) => (
                <div key={i} className="h-12 animate-pulse rounded-md bg-surface-2" />
              ))}
            </div>
          ) : isError ? (
            <p className="p-4 text-[13px] text-faint">Unable to load users.</p>
          ) : data && data.length > 0 ? (
            <table className="w-full text-[13px]">
              <thead>
                <tr className="border-b border-border text-left text-[11px] uppercase tracking-wider text-faint">
                  <th className="px-4 py-2.5 font-medium">User</th>
                  <th className="px-4 py-2.5 font-medium">Email</th>
                  <th className="px-4 py-2.5 font-medium">Role</th>
                  <th className="px-4 py-2.5 font-medium">Joined</th>
                </tr>
              </thead>
              <tbody>
                {data.map((u) => (
                  <tr key={u.id} className="border-b border-border last:border-0 hover:bg-surface-2/50">
                    <td className="px-4 py-2.5">
                      <span className="font-medium text-foreground">
                        {u.first_name} {u.last_name}
                      </span>
                    </td>
                    <td className="px-4 py-2.5 font-mono text-[12px] text-muted">{u.email}</td>
                    <td className="px-4 py-2.5">
                      <Badge tone={u.role === "admin" ? "accent" : "neutral"}>{u.role}</Badge>
                    </td>
                    <td className="px-4 py-2.5 text-muted">{formatDate(u.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState
              icon={<IconUsers className="size-5" />}
              title="No users"
              description="The user management API is not wired up yet."
            />
          )}
        </Card>
      ) : (
        <Card>
          <p className="text-[13px] text-faint">
            You need an admin role to access user management.
          </p>
        </Card>
      )}
    </div>
  )
}