import { createBrowserRouter, Navigate, Outlet, type RouteObject } from "react-router-dom"
import { AppLayout } from "./components/layout/AppLayout"
import { useAuth } from "./features/auth/useAuth"
import { Spinner } from "./components/ui/Button"
import { LoginPage } from "./pages/LoginPage"
import { RegisterPage } from "./pages/RegisterPage"
import { DashboardPage } from "./pages/DashboardPage"
import { ProjectsPage } from "./pages/ProjectsPage"
import { DeploymentsPage } from "./pages/DeploymentsPage"
import { NewRunPage } from "./pages/NewRunPage"
import { RunDetailPage } from "./pages/RunDetailPage"
import { ChatPage } from "./pages/ChatPage"
import { NotificationsPage } from "./pages/NotificationsPage"
import { AlertsPage } from "./pages/AlertsPage"
import { SettingsPage } from "./pages/SettingsPage"
import { AdminPage } from "./pages/AdminPage"

function FullScreenLoader() {
  return (
    <div className="flex min-h-screen items-center justify-center text-muted">
      <Spinner size={22} />
    </div>
  )
}

function ProtectedRoute() {
  const { isAuthenticated, initializing } = useAuth()
  if (initializing) return <FullScreenLoader />
  if (!isAuthenticated) return <Navigate to="/login" replace />
  return <Outlet />
}

function GuestRoute() {
  const { isAuthenticated, initializing } = useAuth()
  if (initializing) return <FullScreenLoader />
  if (isAuthenticated) return <Navigate to="/" replace />
  return <Outlet />
}

export function AppRoutes() {
  const routes: RouteObject[] = [
    {
      element: <GuestRoute />,
      children: [
        { path: "/login", element: <LoginPage /> },
        { path: "/register", element: <RegisterPage /> },
      ],
    },
    {
      element: <ProtectedRoute />,
      children: [
        {
          element: <AppLayout />,
          children: [
            { path: "/", element: <DashboardPage /> },
            { path: "/projects", element: <ProjectsPage /> },
            { path: "/deployments", element: <DeploymentsPage /> },
            { path: "/projects/new", element: <NewRunPage /> },
            { path: "/runs/:jobId", element: <RunDetailPage /> },
            { path: "/chat", element: <ChatPage /> },
            { path: "/notifications", element: <NotificationsPage /> },
            { path: "/alerts", element: <AlertsPage /> },
            { path: "/settings", element: <SettingsPage /> },
            { path: "/admin", element: <AdminPage /> },
          ],
        },
      ],
    },
    { path: "*", element: <Navigate to="/" replace /> },
  ]

  return createBrowserRouter(routes)
}