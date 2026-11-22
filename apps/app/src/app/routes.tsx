import { Navigate, type RouteObject } from "react-router";

import { AuthGate } from "@/auth/auth-gate";
import { LoginPage } from "@/auth/login-page";
import { SetupPage } from "@/auth/setup-page";
import { VerifyEmailPage } from "@/account/verify-email-page";
import { AcceptInvitePage } from "@/invites/accept-invite-page";
import { ForgotPasswordPage } from "@/password-reset/forgot-password-page";
import { ResetPasswordPage } from "@/password-reset/reset-password-page";
import { RevokeTokenPage } from "@/password-reset/revoke-token-page";
import { IndexRedirect } from "@/first-run/index-redirect";
import { AppShell } from "@/components/app-shell";
import { RouteErrorPage } from "@/routes/route-error";

/** Every route in the app. Shared by the browser router and tests (memory router). Pages load lazily. */
export const routes: RouteObject[] = [
  {
    // Where the data source has sign-in, nothing shows until the server says who this is.
    element: (
      <AuthGate>
        <AppShell />
      </AuthGate>
    ),
    // Only if the shell itself fails.
    errorElement: <RouteErrorPage />,
    // Shown while the first lazily loaded page arrives.
    hydrateFallbackElement: (
      <p role="status" className="p-8 text-sm text-muted-foreground">
        Loading NuraMesh…
      </p>
    ),
    children: [
      {
        // Page errors render inside the shell, so the header and landmarks stay.
        errorElement: <RouteErrorPage />,
        children: [
          { index: true, element: <IndexRedirect /> },
          {
            path: "start",
            lazy: async () => ({ Component: (await import("@/first-run/first-run-page")).FirstRunPage }),
          },
          {
            path: "help/approving",
            lazy: async () => ({ Component: (await import("@/routes/approving-help")).ApprovingHelpPage }),
          },
          {
            path: "builder",
            lazy: async () => ({ Component: (await import("@/builder/builder-page")).BuilderPage }),
          },
          {
            path: "templates",
            lazy: async () => ({ Component: (await import("@/templates/templates-page")).TemplatesPage }),
          },
          {
            path: "runs",
            lazy: async () => ({ Component: (await import("@/runs/runs-page")).RunsPage }),
          },
          {
            path: "runs/:id",
            lazy: async () => ({ Component: (await import("@/runs/run-page")).RunPage }),
          },
          {
            path: "runs/:runId/tasks/:taskId",
            lazy: async () => ({ Component: (await import("@/runs/task-audit")).TaskAuditPage }),
          },
          {
            path: "agents",
            lazy: async () => ({ Component: (await import("@/agents/agents-page")).AgentsPage }),
          },
          {
            path: "approvals",
            lazy: async () => ({ Component: (await import("@/approvals/approvals-page")).ApprovalsPage }),
          },
          {
            path: "approvals/:id",
            lazy: async () => ({ Component: (await import("@/approvals/approvals-page")).ApprovalsPage }),
          },
          {
            path: "autonomy",
            lazy: async () => ({ Component: (await import("@/autonomy/autonomy-page")).AutonomyPage }),
          },
          {
            path: "budgets",
            lazy: async () => ({ Component: (await import("@/budgets/budgets-page")).BudgetsPage }),
          },
          { path: "models", element: <Navigate to="/models/catalog" replace /> },
          {
            path: "models/:tab",
            lazy: async () => ({ Component: (await import("@/models/models-page")).ModelsPage }),
          },
          {
            path: "ledger",
            lazy: async () => ({ Component: (await import("@/ledger/ledger-page")).LedgerPage }),
          },
          {
            path: "ledger/:entry",
            lazy: async () => ({ Component: (await import("@/ledger/ledger-page")).LedgerPage }),
          },
          { path: "dashboards", element: <Navigate to="/dashboards/verification" replace /> },
          {
            path: "dashboards/:view",
            lazy: async () => ({ Component: (await import("@/dashboards/dashboards-page")).DashboardsPage }),
          },
          {
            path: "reports/downgrade",
            lazy: async () => ({ Component: (await import("@/reports/downgrade-page")).DowngradePage }),
          },
          {
            path: "settings",
            lazy: async () => ({ Component: (await import("@/settings/settings-page")).SettingsPage }),
          },
          {
            path: "*",
            lazy: async () => ({ Component: (await import("@/routes/not-found")).NotFoundPage }),
          },
        ],
      },
    ],
  },
  // Sign-in screens (after the shell: tests take routes[0] as the shell): no sidebar, reachable without a session.
  { path: "login", element: <LoginPage />, errorElement: <RouteErrorPage /> },
  { path: "verify-email", element: <VerifyEmailPage />, errorElement: <RouteErrorPage /> },
  { path: "accept-invite", element: <AcceptInvitePage />, errorElement: <RouteErrorPage /> },
  { path: "forgot-password", element: <ForgotPasswordPage />, errorElement: <RouteErrorPage /> },
  { path: "reset-password", element: <ResetPasswordPage />, errorElement: <RouteErrorPage /> },
  { path: "revoke-token", element: <RevokeTokenPage />, errorElement: <RouteErrorPage /> },
  { path: "setup", element: <SetupPage />, errorElement: <RouteErrorPage /> },
];
