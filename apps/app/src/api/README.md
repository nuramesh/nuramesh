# The app's API layer

Every screen talks to one typed interface, `ApiClient` (`client.ts`, SPEC-app-v1 §4). Two
implementations sit behind it:

| | where | when |
| --- | --- | --- |
| **Fixture client** | `fixture-client.ts`, seed in `fixtures/seed.ts` (from the design files) | `pnpm dev` by default, unit tests, e2e tests, screenshots |
| **Server client** | `server-client.ts` | production builds by default |

**Choosing:** `VITE_API_SOURCE=fixtures|server`. Unset means fixtures in `pnpm dev` and **server in a
production build**, so a build can never show sample data unless someone asks for it by name
(`chooseSource`, tested). The e2e build asks for fixtures explicitly. The sidebar says "Sample data"
whenever the fixture client is in use.

**Contract:** `contract.ts` holds the tests every client must pass (`runClientContract`). The fixture
client runs them now; the server client will run the same ones against a test server, so switching
never needs a UI change. Money is a decimal string (`money.ts` does the arithmetic in integers).

## The server client

`server-client.ts` talks to `nuramesh serve` (the API notes; how to run both: the app notes). `server/map.ts` holds the pure mappers
from the API's JSON to the screens' types (tested on recorded real answers), `server/live.ts` the per-run WebSocket with
reconnecting, `server/http.ts` the JSON calls and the problem+json errors. What the server has no endpoint for rejects with
`NotAvailableError` (never sample data): `orgs.*`, `config.*`, `templates.*`, `policyPresets.*`, `runs.start`, `runs.pause`.

| Client call | Server |
| --- | --- |
| `runs.listPage`, `runs.get` | `GET /v1/runs` (status filter, `truncated`), `GET /v1/runs/{id}` |
| `runs.events`, `runs.subscribe` | `GET /v1/runs/{id}/ledger` (paged), `WS /v1/runs/{id}/events?after_seq=` |
| `runs.stop` | `POST /v1/runs/{id}/stop` |
| `runs.startPlan`, `runs.startStatus` | `POST /v1/runs` (a plan as JSON, answered 202; the Runs page's Start a run dialog), then `GET /v1/runs/{id}` until the runner has begun or refused it |
| `approvals.*` | `GET /v1/approvals`, `GET /v1/approvals/{id}`, `POST /v1/approvals/{id}/approve` or `/reject` |
| `budgets.list`, `summary`, `unblock` | `GET /v1/budgets`, `GET /v1/budgets/{id}`, `POST .../unblock` (summary is derived from budgets, runs and run details) |
| `spend.byModel` | derived from the run details' `spend.by_model` |
| `ledger.list`, `get` | `GET /v1/ledger` and `/v1/ledger/{stream}` (the newest 200 entries of the 12 most recent streams) |
| `ledger.verifyChain`, `verifyRun` | `GET /v1/ledger/verify`, `GET /v1/runs/{id}/ledger/verify` |
| `org.status` | `GET /v1/org/status` (the halt object: `halted`, `cause`, `roles`, `can_act`...) |
| `org.halt`, `org.resume` | `POST /v1/org/halt`, `POST /v1/org/resume` (a person only; `409 already_halted` / `already_running` is `ConflictError` with that `code`) |
| `org.pauseRole`, `org.resumeRole` | `POST /v1/org/roles/{role}/pause`, `.../resume` (a person only; `409 already_paused` / `not_paused`; `404` is `NotFoundError`) |
| `breakers.list` | `GET /v1/org/breakers` (`org`, `halt`, `evaluation`, `breakers`) |
| `breakers.reset` | `POST /v1/org/breakers/{name}/reset` (a person only; `409 not_tripped`) |
| `breakers.disable`, `enable` | `POST /v1/org/breakers/{name}/disable` (body `{"reason": "false_positive" \| "planned_work" \| "rule_being_replaced" \| "other"}`; `409 already_disabled`, `breaker_tripped`), `.../enable` (`409 not_disabled`); a person only |
| `agentCredentials.agents` | `GET /v1/agents` (owner only: `{id, name, role, active_credentials}` per agent; the Agents page list) |
| `agentCredentials.callerRole` | `GET /v1/auth/session` (`user.role`; `null` when there is no session, then the server decides and its refusal shows the same sentence) |
| `agentCredentials.list` | `GET /v1/agents/{id}/credentials`, `GET /v1/agent-credentials?issuer_erased=true` (owner only; a `403` is `NotAllowedError`) |
| `agentCredentials.issue`, `rotate` | `POST /v1/agents/{id}/credentials`, `POST .../{credential}/rotate` (the secret comes back once as `credential`; `409 agent_suspended` while halted or the role is paused, `409 credential_limit`: `ConflictError` with that `code`) |
| `agentCredentials.revoke`, `revokeAll` | `POST .../{credential}/revoke`, `POST .../revoke-all` (never refused while halted) |
| `invites.list`, `create`, `revoke`, `resend` | `GET /v1/auth/invites`, `POST /v1/auth/invites`, `POST .../{id}/revoke`, `POST .../{id}/resend` (owner session; the list never carries a token or link; `503 email_not_configured` is shown as a banner in Settings > People) |
| `passwordReset.request`, `redeem`, `revokeByLink` | `POST /v1/auth/password-reset/request` (always 202), `POST /v1/auth/password-reset/redeem`, `POST /v1/auth/tokens/revoke-by-link` (public; the tokens come from the `#t=` fragment of `/reset-password` and `/revoke-token`, are cleared from the address at once and kept in memory only; every bad link is one `403`) |
| `invites.inspect`, `accept` | `POST /v1/auth/invites/inspect`, `POST /v1/auth/invites/accept` (public; the token comes from the `#t=` fragment of `/accept-invite`, is cleared from the address at once and kept in memory only; every bad link is one `403 invalid_invite`) |
| `auth.mfaStatus`, `mfaEnroll`, `mfaConfirm`, `mfaRegenerate`, `mfaDisable` | `GET /v1/auth/mfa`, `POST /v1/auth/mfa/enroll`, `.../enroll/confirm`, `/v1/auth/mfa/recovery-codes`, `/v1/auth/mfa/disable` (on the sign-in object, not `ApiClient`; a person's session only. The three that return a secret or recovery codes are used only when the answer carries `Cache-Control: no-store`, are never repeated after a 403 `csrf_invalid`, and their answers live in React state of Settings > Security only: never the query cache, storage, the tab channel or a log) |
| `auth.confirmPassword` | `POST /v1/auth/password/confirm` (solo approval SA-8; on the sign-in object, a person's session only, body `{password}`, no idempotency key. Called only from the solo approve dialog of the approvals page, before a sole decider's approve of a high-risk kind; the password lives in that dialog's React state until the call returns and nowhere else) |
| `auth.login` (second step), `completeMfa`, `loginEnroll`, `loginEnrollConfirm` | `POST /v1/auth/login` answers `signed-in`, `mfa_required` or `mfa_enroll_required` (`mfa_enroll_required` carries a challenge only when it is well formed; the enrolment screens are offered only then), then `POST /v1/auth/login/mfa` (`completeMfa`, the code step of the login page), `.../login/mfa/enroll`, `.../login/mfa/enroll/confirm` (the two enrolment routes are public: their own client, no CSRF header, never repeated; the challenge is kept in memory of the login page only, never in the address, history or storage; `loginEnroll` and `loginEnrollConfirm` are used only with `Cache-Control: no-store`, and the confirm signs nobody in until the person presses Continue and the session is read again) |
| `info` | `GET /v1/health` |

The nine org routes are E19 (the organization safety notes section 11). Anything that loosens protection (resume, role resume,
reset, disable, enable) is a person's alone: a `403 human_required` or `forbidden` is a `NotAllowedError` whose `code` is the
server's, and the screens show those buttons only when `can_act` is true (`OrgStatus.canAct`, which the mapper sets to false
unless the server says true). The fixture client takes `createFixtureClient({ orgSafety })` with `running` (default),
`halted`, `tripped`, `failing`, `poison`, `rolePaused`, `disabled`, `empty` or `viewer`; the fixture app build also reads
`?orgSafety=<state>` from the address (fixture data only). Both clients pass `org-safety-contract.ts` (the server client over
`src/test/fixture-backed-server.ts`, a fetch that answers the nine routes from a fixture client).

## What stays on fixtures (sample data) until the server has it

Run start and pause; templates and preset storage; org read and update; YAML import and export; the org switcher.
