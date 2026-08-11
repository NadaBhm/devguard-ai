import { NavLink, Outlet } from "react-router-dom"
import { useAuth } from "../../features/auth/useAuth"
import {
  IconBell,
  IconChart,
  IconCost,
  IconDeploy,
  IconGauge,
  IconLogout,
  IconRepo,
  IconShield,
} from "../icons"

function BrandMark() {
  return (
    <div className="flex size-8 items-center justify-center rounded-lg border border-border bg-surface-2">
      <IconShield className="size-4.5 text-accent" />
    </div>
  )
}

interface NavItem {
  to: string
  label: string
  icon: React.ComponentType<React.SVGProps<SVGSVGElement>>
  end?: boolean
}

const primaryNav: NavItem[] = [
  { to: "/", label: "Dashboard", icon: IconGauge, end: true },
  { to: "/projects", label: "Projects", icon: IconRepo },
  { to: "/deployments", label: "Deployments", icon: IconDeploy },
  { to: "/alerts", label: "Cost alerts", icon: IconBell },
  { to: "/notifications", label: "Activity", icon: IconChart },
]

const secondaryNav: NavItem[] = [
  { to: "/settings", label: "Settings", icon: IconCost },
]

function NavLinks({ items }: { items: NavItem[] }) {
  return (
    <>
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          className={({ isActive }) =>
            `group flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px] transition-colors ${
              isActive
                ? "bg-surface-2 font-medium text-foreground"
                : "text-muted hover:bg-surface-2/60 hover:text-foreground"
            }`
          }
        >
          <item.icon
            className={`size-4 ${"text-faint"}`}
          />
          <span>{item.label}</span>
        </NavLink>
      ))}
    </>
  )
}

export function AppLayout() {
  const { user, logout } = useAuth()

  return (
    <div className="flex min-h-screen">
      <aside className="fixed inset-y-0 left-0 z-30 flex w-60 flex-col border-r border-border bg-surface">
        <div className="flex items-center gap-3 border-b border-border px-4 py-3.5">
          <BrandMark />
          <div className="leading-tight">
            <p className="text-[13px] font-semibold tracking-tight text-foreground">DevGuard AI</p>
            <p className="text-[11px] text-faint">dga</p>
          </div>
        </div>

        <nav className="flex-1 space-y-0.5 overflow-y-auto px-2.5 py-3">
          <p className="px-2.5 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-faint">
            Analyze
          </p>
          <NavLinks items={primaryNav} />
        </nav>

        <div className="border-t border-border px-2.5 py-3">
          <NavLinks items={secondaryNav} />
        </div>

        <footer className="border-t border-border px-2.5 py-3">
          <div className="flex items-center gap-2.5 rounded-md px-2.5 py-1.5">
            <span className="flex size-7 items-center justify-center rounded-full border border-border bg-raised text-[11px] font-semibold uppercase text-accent">
              {user ? (user.first_name?.[0] ?? "D") : "D"}
            </span>
            <div className="min-w-0 flex-1 leading-tight">
              <p className="truncate text-[12px] font-medium text-foreground">
                {user ? `${user.first_name} ${user.last_name}` : "Signing in…"}
              </p>
              <p className="truncate text-[11px] text-faint">{user?.email}</p>
            </div>
            <button
              type="button"
              onClick={logout}
              title="Sign out"
              className="text-faint transition-colors hover:text-critical"
            >
              <IconLogout className="size-4" />
            </button>
          </div>
        </footer>
      </aside>

      <div className="flex-1 pl-60">
        <main className="mx-auto w-full max-w-[1100px] px-6 py-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}