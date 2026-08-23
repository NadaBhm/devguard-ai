import { useCallback, useState } from "react"
import { NavLink, Outlet } from "react-router-dom"
import { useAuth } from "../../features/auth/useAuth"
import {
  IconBell,
  IconChart,
  IconChevronLeft,
  IconChevronRight,
  IconCost,
  IconDeploy,
  IconGauge,
  IconLogout,
  IconRepo,
  IconShield,
  IconUsers,
} from "../icons"

function BrandMark() {
  return (
    <div className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-border bg-surface-2">
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
  { to: "/profile", label: "Profile", icon: IconUsers },
  { to: "/settings", label: "Settings", icon: IconCost },
]

function NavLinks({ items, collapsed }: { items: NavItem[]; collapsed: boolean }) {
  return (
    <>
      {items.map((item) => (
        <NavLink
          key={item.to}
          to={item.to}
          end={item.end}
          title={collapsed ? item.label : undefined}
          className={({ isActive }) =>
            `group relative flex items-center rounded-md transition-colors duration-150 ${
              collapsed ? "justify-center py-2.5" : "gap-2.5 px-2.5 py-1.5 text-[13px]"
            } ${
              isActive
                ? "bg-surface-2 font-medium text-foreground"
                : "text-muted hover:bg-surface-2/60 hover:text-foreground"
            }`
          }
        >
          <item.icon className={`shrink-0 ${collapsed ? "size-5" : "size-4"} text-faint`} />
          {collapsed ? (
            <span
              className={`pointer-events-none absolute left-full top-1/2 z-50 ml-2.5 -translate-y-1/2 translate-x-1 whitespace-nowrap rounded-md border border-border bg-surface-2 px-2.5 py-1 text-[12px] font-medium text-foreground opacity-0 shadow-md transition-all duration-150 group-hover:translate-x-0 group-hover:opacity-100`}
            >
              {item.label}
            </span>
          ) : (
            <span className="truncate">{item.label}</span>
          )}
        </NavLink>
      ))}
    </>
  )
}

const STORAGE_KEY = "devguard:sidebar-collapsed"

export function AppLayout() {
  const { user, logout } = useAuth()
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(STORAGE_KEY) === "1"
    } catch {
      return false
    }
  })

  const toggleCollapsed = useCallback(() => {
    setCollapsed((c) => {
      const next = !c
      try {
        localStorage.setItem(STORAGE_KEY, next ? "1" : "0")
      } catch {
        // storage may be unavailable; treat as off
      }
      return next
    })
  }, [])

  return (
    <div className="flex min-h-screen">
      <aside
        className={`fixed inset-y-0 left-0 z-30 flex flex-col border-r border-border bg-surface transition-[width] duration-300 ease-in-out ${
          collapsed ? "w-16" : "w-60"
        }`}
      >
        <div
          className={`relative flex shrink-0 items-center border-b border-border transition-[padding] duration-300 ${
            collapsed ? "justify-center gap-0 px-0 py-4" : "gap-3 px-4 py-3.5"
          }`}
        >
          <BrandMark />
          <div
            className={`min-w-0 overflow-hidden leading-tight transition-[width,opacity] duration-300 ${
              collapsed ? "w-0 opacity-0" : "w-auto opacity-100"
            }`}
          >
            <p className="text-[13px] font-semibold tracking-tight text-foreground">DevGuard AI</p>
            <p className="text-[11px] text-faint">dga</p>
          </div>

          <button
            type="button"
            onClick={toggleCollapsed}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className="absolute top-1/2 right-0 z-40 flex size-6 -translate-y-1/2 translate-x-1/2 items-center justify-center rounded-full border border-border bg-surface-2 text-faint shadow-sm transition-colors duration-150 hover:border-border-strong hover:text-foreground"
          >
            {collapsed ? <IconChevronRight className="size-3.5" /> : <IconChevronLeft className="size-3.5" />}
          </button>
        </div>

        <nav
          className={`flex-1 space-y-0.5 px-2.5 py-3 transition-[overflow] duration-300 ${
            collapsed ? "overflow-visible" : "overflow-y-auto"
          }`}
        >
          {!collapsed && (
            <p className="px-2.5 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-faint">
              Analyze
            </p>
          )}
          <NavLinks items={primaryNav} collapsed={collapsed} />
        </nav>

        <div className="shrink-0 border-t border-border px-2.5 py-3">
          <NavLinks items={secondaryNav} collapsed={collapsed} />
        </div>

        <footer className="shrink-0 border-t border-border px-2.5 py-3">
          <div
            className={`flex items-center rounded-md py-1 transition-[justify-content,padding] duration-300 ${
              collapsed ? "justify-center gap-2 px-0" : "gap-1 px-2"
            }`}
          >
            <NavLink
              to="/profile"
              title="Profile"
              className={({ isActive }) =>
                `flex items-center rounded-md transition-colors ${
                  collapsed ? "px-1 py-0.5" : "min-w-0 flex-1 gap-2.5 px-1.5 py-1"
                } ${isActive ? "bg-surface-2" : "hover:bg-surface-2/60"}`
              }
            >
              <span className="flex size-7 shrink-0 items-center justify-center rounded-full border border-border bg-raised text-[11px] font-semibold uppercase text-accent">
                {user ? (user.first_name?.[0] ?? "D") : "D"}
              </span>
              {!collapsed && (
                <span className="min-w-0 flex-1 leading-tight">
                  <span className="block truncate text-[12px] font-medium text-foreground">
                    {user ? `${user.first_name} ${user.last_name}` : "Signing in…"}
                  </span>
                  <span className="block truncate text-[11px] text-faint">{user?.email}</span>
                </span>
              )}
            </NavLink>
            {!collapsed && (
              <button
                type="button"
                onClick={logout}
                title="Sign out"
                className="shrink-0 rounded-md p-1.5 text-faint transition-colors hover:text-critical"
              >
                <IconLogout className="size-4" />
              </button>
            )}
          </div>
        </footer>
      </aside>

      <div
        className={`min-w-0 flex-1 transition-[padding] duration-300 ease-in-out ${
          collapsed ? "pl-16" : "pl-60"
        }`}
      >
        <main className="mx-auto w-full max-w-[1100px] px-6 py-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
