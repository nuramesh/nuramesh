import { ConflictError, NotAllowedError, NotFoundError, type ApiClient } from "@/api/client";
import { createFixtureClient, type OrgSafetyScenario } from "@/api/fixture-client";
import { createServerClient } from "@/api/server-client";
import type { RawBreaker, RawBreakers, RawHalt } from "@/api/server/raw";
import type { BreakerRow, DisableReason, OrgStatus } from "@/api/types";

/** The camel-case halt object back to the server's snake-case form (the inverse of `orgStatus`). */
export function rawHalt(s: OrgStatus, extra: Partial<RawHalt> = {}): RawHalt {
  return {
    halted: s.halted,
    by: s.by,
    at: s.at,
    cause: s.cause,
    rule: s.rule,
    ledger_seq: s.ledgerSeq,
    phase: s.phase,
    error_class: s.errorClass,
    failing_rule: s.failingRule,
    failed_ticks: s.failedTicks,
    roles: s.roles.map((r) => ({ role: r.role, paused: r.paused, by: r.by, at: r.at, cause: r.cause, rule: r.rule, ledger_seq: r.ledgerSeq, in_config: r.inConfig })),
    can_act: s.canAct,
    // The status read's `solo_approval` (SA-18 item 4); the app shows only the last count.
    solo_approval: {
      solo_now: false,
      limit_per_hour: 30,
      window_days: 30,
      approved: s.soloAfterRemoval,
      approved_solo: s.soloAfterRemoval,
      solo_share: s.soloAfterRemoval > 0 ? "1.0000" : null,
      approved_solo_after_removal: s.soloAfterRemoval,
    },
    ...extra,
  };
}

export function rawBreaker(b: BreakerRow): RawBreaker {
  return {
    name: b.name,
    signal: b.signal,
    action: b.action,
    role: b.role,
    window_seconds: b.windowSeconds,
    dry_run: b.dryRun,
    limit: b.limit,
    observed: b.observed,
    observed_as: b.observedAs,
    unknown_cost_calls: b.unknownCostCalls,
    window_start: b.windowStart,
    state: b.state,
    tripped_at: b.trippedAt,
    tripped_seq: b.trippedSeq,
    tripped_by: b.trippedBy,
    manual: b.manual,
    last_reset_at: b.lastResetAt,
    disabled_at: b.disabledAt,
    disabled_by: b.disabledBy,
    disabled_reason: b.disabledReason,
    in_config: b.inConfig,
  };
}

function problem(status: number, title: string, detail: string): Response {
  return new Response(JSON.stringify({ type: `urn:nuramesh:${title}`, title, status, detail }), { status, headers: { "content-type": "application/problem+json" } });
}

/**
 * A fetch that answers the nine org routes (`spec/e19/README.md` section 11) from a fixture client's
 * state, so the server client can run the same contract tests as the fixture client. A test of the
 * client's own wire format uses `calls`, which records every request.
 */
export function fixtureBackedFetch(scenario?: OrgSafetyScenario) {
  const fixture = createFixtureClient(scenario === undefined ? {} : { orgSafety: scenario });
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchFn = async (input: string, init?: RequestInit): Promise<Response> => {
    const url = new URL(input, "http://127.0.0.1:5173");
    const method = init?.method ?? "GET";
    const body: unknown = init?.body === undefined ? undefined : JSON.parse(init.body as string);
    calls.push({ method, path: url.pathname, body });
    const json = (value: unknown): Response => new Response(JSON.stringify(value), { status: 200, headers: { "content-type": "application/json" } });
    try {
      const path = url.pathname;
      if (method === "GET" && path === "/v1/org/status") return json(rawHalt(await fixture.org.status()));
      if (method === "POST" && path === "/v1/org/halt") {
        const { stoppedRuns, ...status } = await fixture.org.halt();
        return json(rawHalt(status, { stopped_runs: stoppedRuns }));
      }
      if (method === "POST" && path === "/v1/org/resume") return json(rawHalt(await fixture.org.resume()));
      if (method === "GET" && path === "/v1/org/breakers") {
        const view = await fixture.breakers.list();
        const raw: RawBreakers = {
          org: view.org,
          halt: rawHalt(view.halt),
          evaluation: {
            state: view.evaluation.state,
            tick_seconds: view.evaluation.tickSeconds,
            halt_after_failed_ticks: view.evaluation.haltAfterFailedTicks,
            last_evaluated_at: view.evaluation.lastEvaluatedAt,
            last_error_at: view.evaluation.lastErrorAt,
            last_error_phase: view.evaluation.lastErrorPhase,
            last_error_class: view.evaluation.lastErrorClass,
            last_failing_rule: view.evaluation.lastFailingRule,
            consecutive_failures: view.evaluation.consecutiveFailures,
          },
          breakers: view.breakers.map(rawBreaker),
        };
        return json(raw);
      }
      const role = /^\/v1\/org\/roles\/([^/]+)\/(pause|resume)$/.exec(path);
      if (method === "POST" && role !== null) {
        const name = decodeURIComponent(role[1] ?? "");
        if (role[2] === "pause") {
          const { stoppedTasks, ...status } = await fixture.org.pauseRole(name);
          return json(rawHalt(status, { stopped_tasks: stoppedTasks }));
        }
        return json(rawHalt(await fixture.org.resumeRole(name)));
      }
      const breaker = /^\/v1\/org\/breakers\/([^/]+)\/(reset|disable|enable)$/.exec(path);
      if (method === "POST" && breaker !== null) {
        const name = decodeURIComponent(breaker[1] ?? "");
        if (breaker[2] === "reset") {
          const r = await fixture.breakers.reset(name);
          return json({ name: r.name, state: r.state, reset_at: r.resetAt, ledger_seq: r.ledgerSeq, org_halted: r.orgHalted });
        }
        if (breaker[2] === "disable") {
          const reason = (body as { reason?: DisableReason } | undefined)?.reason;
          if (reason === undefined) return problem(422, "validation", "reason is required");
          const r = await fixture.breakers.disable(name, reason);
          return json({ name: r.name, state: r.state, disabled_at: r.disabledAt, reason: r.reason, ledger_seq: r.ledgerSeq });
        }
        const r = await fixture.breakers.enable(name);
        return json({ name: r.name, state: r.state, enabled_at: r.enabledAt, ledger_seq: r.ledgerSeq });
      }
      return problem(404, "not_found", "no route");
    } catch (error) {
      if (error instanceof ConflictError) return problem(409, error.code, error.message);
      if (error instanceof NotAllowedError) return problem(403, error.code, error.message);
      if (error instanceof NotFoundError) return problem(404, "no_such_thing", error.message);
      throw error;
    }
  };
  return { fetchFn, calls, fixture };
}

/** The server client talking to `fixtureBackedFetch`. */
export function fixtureBackedServerClient(scenario?: OrgSafetyScenario): ApiClient {
  return createServerClient({ fetch: fixtureBackedFetch(scenario).fetchFn });
}
