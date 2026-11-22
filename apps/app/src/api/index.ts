import { type AgentAccessScenario, AGENT_ACCESS_SCENARIOS, agentAccessOptions } from "./fixtures/agent-access";
import type { ApiClient } from "./client";
import type { OrgSafetyScenario } from "./fixtures/org-safety";
import { type DataSource, saveSource, storedServerUrl, storedSource } from "./settings";
import { createServerClient } from "./server-client";

export type { DataSource } from "./settings";

/**
 * Which data source to use. `VITE_API_SOURCE` says it explicitly; else the choice made in Settings
 * (kept in this browser). Without either, the dev server uses
 * fixtures (so the app works before the server does) and **a production build uses the server**:
 * a build can never show sample data unless someone asked for it by name.
 */
export function chooseSource(configured: string | undefined, isDev: boolean, stored: DataSource | null = null): DataSource {
  if (configured === "fixtures" || configured === "server") return configured;
  if (configured !== undefined && configured !== "") {
    throw new Error(`VITE_API_SOURCE must be "fixtures" or "server", not "${configured}"`);
  }
  return isDev ? (stored ?? "fixtures") : "server";
}

/** The stored choice; a production build ignores it and removes a stale `fixtures` key (#888). */
function storedSourceForBuild(isDev: boolean): DataSource | null {
  const stored = storedSource();
  if (!isDev && stored === "fixtures") saveSource(null);
  return isDev ? stored : null;
}

/** True when the build names its source (`VITE_API_SOURCE`), so the Settings choice does not apply. */
export function sourcePinned(configured: string | undefined = import.meta.env.VITE_API_SOURCE as string | undefined): boolean {
  return configured === "fixtures" || configured === "server";
}

/** Builds the client for the chosen source (the fixture client is loaded only when asked for). */
export async function createApiClient(
  source: DataSource = chooseSource(
    import.meta.env.VITE_API_SOURCE as string | undefined,
    import.meta.env.DEV,
    storedSourceForBuild(import.meta.env.DEV),
  ),
): Promise<ApiClient> {
  if (source === "server") {
    const configured = (import.meta.env.VITE_API_URL as string | undefined) ?? "";
    return createServerClient({ baseUrl: storedServerUrl() || configured });
  }
  // A build pinned to the server (`VITE_API_SOURCE=server`, as the Compose web image is) replaces this
  // constant at build time, so the bundler drops the import below and no fixture chunk is emitted (#888).
  if (import.meta.env.VITE_API_SOURCE === "server") {
    throw new Error("This build is pinned to the server and carries no sample data");
  }
  const { createFixtureClient } = await import("./fixture-client");
  const role = import.meta.env.VITE_FIXTURE_ROLE as string | undefined;
  const scenario = fixtureOrgSafety(globalThis.location.search);
  const access = fixtureAgentAccess(globalThis.location.search);
  return createFixtureClient({
    ...(role === "viewer" || role === "approver" ? { role } : {}),
    ...(scenario === undefined ? {} : { orgSafety: scenario }),
    ...(access === undefined ? {} : { agentAccess: agentAccessOptions(access) }),
  });
}

/** `?agentAccess=erased` picks the sample credentials and caller of the Agents page (fixture data only). */
export function fixtureAgentAccess(search: string): AgentAccessScenario | undefined {
  const wanted = new URLSearchParams(search).get("agentAccess");
  return AGENT_ACCESS_SCENARIOS.find((s) => s === wanted);
}

const ORG_SAFETY_SCENARIOS: readonly OrgSafetyScenario[] = ["running", "halted", "tripped", "failing", "poison", "rolePaused", "disabled", "empty", "viewer"];

/**
 * `?orgSafety=halted` picks the sample state of the kill switch and breakers in the fixture data
 * (for the screenshots and the accessibility checks); it never applies to the server client.
 */
export function fixtureOrgSafety(search: string): OrgSafetyScenario | undefined {
  const wanted = new URLSearchParams(search).get("orgSafety");
  return ORG_SAFETY_SCENARIOS.find((s) => s === wanted);
}

export * from "./client";
export * from "./money";
export * from "./models";
export * from "./reports";
export * from "./savings";
export * from "./types";
export * from "./dashboards";
