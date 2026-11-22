# TypeScript SDK (R2.12)

`apps/sdk-ts` (`@nuramesh/sdk`) is a typed client for the REST API (`docs/api.md`), generated from the server's OpenAPI schema. It is `private: true`: it is built and tested locally and **not published** (publishing a package needs the owner's word). It has no runtime dependencies, uses the global `fetch` (Node 24 or a browser) and sends no telemetry: the only request is the one you make.

```ts
import { NuraMeshClient, NuraMeshError, isErrorCode } from "@nuramesh/sdk";

const nura = new NuraMeshClient({ baseUrl: "http://127.0.0.1:8765", token: process.env.NURAMESH_TOKEN });

const runs = await nura.listRuns();
const run = await nura.getRun({ path: { run_id: "run-1" } });
await nura.approveApproval({
  path: { approval_id: "ap-1" },
  headers: { "Idempotency-Key": crypto.randomUUID() },
  body: { reason: "checked the diff" },
});

try {
  await nura.stopRun({ path: { run_id: "run-1" }, body: { reason: "operator" } });
} catch (e) {
  if (e instanceof NuraMeshError && e.code === "conflict") { /* e.status, e.detail, e.extra */ }
}
```

- **Methods:** one per operation, named by one rule: the server's operation id is **verb-first snake_case** (`<verb>_<noun phrase>`, from the one table `src/nuramesh/server/operation_ids.py`) and the method is its lowerCamelCase: `GET /v1/runs/{run_id}` is `get_run` -> `getRun`, `PUT /v1/org/prompt-cache` is `setOrgPromptCache`. The verbs are a closed list in that module: `get` (one resource or a computed view), `list` (a collection, plural noun), `create` (a POST that makes a resource), `set` (a PUT of the whole value), `update`, `delete` (a DELETE or a `.../remove` action), and the action verbs (`start`, `stop`, `halt`, `resume`, `pause`, `retire`, `enable`, `disable`, `reset`, `unblock`, `approve`, `reject`, `revoke`, `rotate`, `issue`, `enroll`, `confirm`, `regenerate`, `login`, `logout`, `refresh`, `redeem`, `request`, `inspect`, `accept`, `resend`, `verify`, `validate`, `quote`, `test`, `reveal`; `decide` only on deprecated twins). The noun phrase names the resource as a developer says it, the parent kept where it reads naturally (`listRunTasks`, `getTaskTimeline`, `revokeAllAgentCredentials`). A deprecated route's method ends in `Deprecated` and carries `@deprecated`. `tests/test_sdk_names.py` fails on a route without a row, an id outside the rule, a stale `generated.ts` or a drifted rename table (`docs/upgrade-notes.md`, "SDK method names"). The full list is `OPERATIONS` in `src/generated.ts`. An operation takes one `args` object with `path`, `query`, `headers` (such as `Idempotency-Key`) and `body` (only the parts it has), then optional `{ signal, headers }`. Responses are typed where the server declares a response model and `unknown` or `Record<string, unknown>` elsewhere; the SDK does not guess.
- **Errors:** every failed call throws `NuraMeshError` with `status`, `code`, `detail` and `extra`. `code` is one of `ERROR_CODES` (`not_found`, `conflict`, `rate_limited`, `entitlement_required`, ...) from the `application/problem+json` answer; FastAPI validation answers (422) become `invalid_request`, a non-problem failure `unknown_error` and a network failure (`status` 0) `network_error`.
- **Auth:** pass `token` for a bearer credential, or `credentials: "include"` for the session cookie. The server's auth is still being finished (R1.22): after it lands, regenerate (below) and the types follow.

## Regenerating

```sh
uv run python scripts/sdk/dump_openapi.py      # server -> apps/sdk-ts/openapi.json (sorted, stable)
cd apps/sdk-ts && pnpm gen                     # openapi.json -> src/generated.ts
```

`tests/test_ts_sdk_stale.py` fails when either committed file differs from a fresh run, and when the server raises an error code that `src/errors.ts` does not list. Checks: `pnpm typecheck`, `pnpm lint`, `pnpm test` (a fake `fetch`, no network). Why our own generator and not a package: an internal decision record.
