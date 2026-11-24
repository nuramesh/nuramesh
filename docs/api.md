# API contract notes for the app (live run, Stage A of the v0.1 board)

The REST API is under `/v1`, errors are `application/problem+json` (`{"type": "urn:nuramesh:<code>", "code": "<code>", "title": "<code>", "status": 409, "detail": "..."}`), and the schema is at `/v1/openapi.json` (a test builds it and checks every `$ref`, so the app can generate types from it). **`entitlement_required`** (403, with a `key` field) is the one code a commercial feature (enterprise or cloud) answers when it is not granted; the open core never returns it. It is 403, not 402: no payment is implied. It has **no business rules**: every value below is derived from the store and the ledger with the engine's own models. The ledger holds hashes, never prompts or answers, so none of these endpoints can show them.

**Loopback by default.** `nuramesh serve` listens on loopback (`127.0.0.1`, `::1`, `localhost`) and answers only requests that name a loopback `Host`. Authentication is on: the default is on (`--auth on|off`; a bare `--auth` means on). A network address is accepted only with authentication on plus `--trusted-proxy` and at least one `--allowed-host`; otherwise it exits with an explanation. With `--auth off` (typed on purpose, loopback only; refused with a network address, `--allowed-host`, `--trusted-proxy` or a hosted deployment) reads are open and every write (stop, approval decision, budget unblock) is refused with 403 `authentication_required`; nothing acts as a person. The actor is never read from a body or header. The browser app has a login screen (R1.22 PR 4).

## Versioning, pages and errors (the promises every route keeps; they follow the CLI and API naming spec)

- **`NuraMesh-Version`.** The path prefix `/v1` never changes; the shapes under it are selected by this request header. Its one value today is `2026-12`; a client that sends it gets the shapes below, a client that sends no header gets the shapes the routes answered before (named list keys, bare arrays) with `Deprecation`, `Sunset` and a `Link rel="deprecation"` to the upgrade notes. Any other value, or two values, is refused before anything runs with 400 `version_unsupported` (the detail names the supported values). Every `/v1` answer, errors included, carries the header with the version it used; other paths never do. The schema's `info.version` is `1` (the prefix) and its `x-nuramesh-contract` member is the current header value.
- **One page shape.** Every collection that grows with use (runs, tasks, timelines, ledger streams and events, approvals, budgets, agents and agent credentials, users, tokens, invites, grants, cost calls, catalog and connection events) takes `limit` (1 to 200, default 50, with four exceptions below) and `cursor` and, under the header, answers `{"items": [...], "next_cursor": "<opaque>" | null}`: `items` is the page, `next_cursor` the cursor of the next one or `null` on the last. The four exceptions: `/v1/ledger/{stream}` and `/v1/runs/{run_id}/ledger` take `limit` 1 to 500, default 100; `/v1/models/catalog/events` takes 1 to 1000, default 500; `/v1/ledger/verify` takes 1 to 100, default 50. A cursor is opaque text this route issued; it holds no scope and no data (every page is read again under the caller's own permissions). A cursor from another route, or one whose row is gone, is 400 `cursor_invalid`. The older parameters `after` (cost calls, verify), `after_seq` (ledger pages) and `before` (event histories) are deprecated: they work alone for one more release, and with `cursor` together they are 400 `invalid_request`. Without the header and without `limit` or `cursor`, a route that answered the whole list keeps doing so.
- **Errors.** Every error is `application/problem+json` with `type` (`urn:nuramesh:<code>`), `code`, `title`, `status` and `detail`, plus extension members where documented (`problems` on a 422, `key` on `entitlement_required`, `retry_after`). Clients key on `code` (or `type`): it is the stable machine name; `title` is text (it equals `code` today and becomes a human title in a later release). The generated SDK lists every `code` (`apps/sdk-ts/src/errors.ts`).
- **Statuses.** A read is 200. A write that creates a record answers 201 (`POST /v1/auth/tokens`, `/v1/auth/invites` and `.../invites/accept`, `/v1/auth/setup/redeem`, `/v1/agents/{id}/credentials` and `.../rotate`, `/v1/autonomy/grants`, `/v1/models/catalog/entries`, `/v1/models/catalog/prices`, `PUT /v1/models/connections/{provider}/key` for a first key, a new gate decision on the route and on its deprecated twin `.../gates/{gate_id}/decision`) and 200 when the same request finds the record already there; a write that only starts work answers 202 (`POST /v1/runs`, an email change, a password-reset request, an owner's password reset: the result arrives later); every other action, halt and stop included, answers 200 because it acts at once. The schema lists the statuses per operation; the route table below is the per-route contract.

## Accounts and sessions (R1.22 PR 2; spec: the internal specification)

**Enforced by default.** With `--auth off` these endpoints exist but no route requires a session and every write is refused. With authentication on every route follows the authorization matrix (an internal document §11), actions are recorded as the signed-in person or the API token's service actor, and a Host allow-list applies. The app's login screen is R1.22 PR 4.

| Method and path | What it does |
| --- | --- |
| `POST /v1/auth/login` `{email, password}` | 200 with `{user, session, csrf_token}` and three cookies, or 200 `{status: mfa_required, challenge, methods, expires_in}` (a person with MFA) or `{status: mfa_enroll_required, challenge, expires_in}` (an owner under the policy) with no cookie and no session; 401 `invalid_credentials` (one message for every cause); 429 when locked, rate limited or the hashing budget is busy |
| `POST /v1/auth/login/mfa` `{challenge, code}` or `{challenge, recovery_code}` | second step of a login that answered `mfa_required`: 200 as the login body; 401 `challenge_invalid` (one answer for an unknown, used, expired or wrong challenge), 401 `mfa_invalid`, 429 `too_many_attempts`, 503 `mfa_unavailable` (TOTP only) |
| `POST /v1/auth/login/mfa/enroll` `{challenge}` | after `mfa_enroll_required`: 200 `{factor_id, secret, otpauth_uri}` (`Cache-Control: no-store`); 401 `challenge_invalid`; 503 `mfa_unavailable` |
| `POST /v1/auth/login/mfa/enroll/confirm` `{challenge, factor_id, code}` | 200 the login body plus `recovery_codes` (10); 401 `challenge_invalid` or `mfa_invalid`; 429 `too_many_attempts` |
| `POST /v1/auth/refresh` `{}` | rotates the access and refresh tokens (needs the CSRF header); a refresh token used twice revokes the whole session, except inside the grace window (below) |
| `POST /v1/auth/logout` `{}` | revokes the session, 204, clears the cookies (needs the CSRF header) |
| `GET /v1/auth/session` | who am I (no secrets), and the CSRF token to send back |
| `GET /v1/auth/setup/status` | public, read-only: `{"owner_missing": true}` while no owner has ever been made, false forever after (T111); counts against the client's rate limit |
| `POST /v1/auth/setup/redeem` `{token, email, display_name?, password}` | redeems the one-time first-owner setup token and creates the owner (201); no session is started; a `Sec-Fetch-Site` header other than `same-origin` or `none` is 403 `foreign_origin`, checked with the `Origin` (order: Host 400, Origin and `Sec-Fetch-Site` 403, content type 415, body 422, then the token) |
| `GET /setup` | the API's own first-owner page (HTML, not part of the OpenAPI description): reads the setup token from the URL fragment (`/setup#token=...`, as `nuramesh serve` prints it), removes the fragment and sends the token only in the JSON body of the redeem; own CSP with a hash for its one inline script and one inline style, `Cache-Control: no-store`; 404 while authentication is off and for ever once an owner record has existed; counts against the client's rate limit |
| `GET /v1/auth/invites` | owner in a human session: `{invites: [view]}`, newest first, all statuses; `view` = `{id, email (null once accepted), role, status, created_by, created_at, expires_at, accepted_user_id, resent_count}`; never a token |
| `POST /v1/auth/invites` `{email, role}` | owner: creates and emails an invitation (201 `{invite: view}`); role `viewer` or `approver`; 503 `email_not_configured`, 409 `already_exists` / `invite_pending` / `invite_limit`, 429 `rate_limited` (30 a hour), 502 `email_send_failed` |
| `POST /v1/auth/invites/{invite_id}/revoke` `{}` | owner: revokes a pending invitation (200 `{invite, changed}`); honors `Idempotency-Key`; unknown id 404 |
| `POST /v1/auth/invites/{invite_id}/resend` `{}` | owner: new link and expiry for a pending invitation, at most 5 times (200 `{invite: view}`); 409 `invite_not_pending` / `resend_limit` / `creator_inactive` |
| `POST /v1/auth/invites/inspect` `{token}` | public: `{email, role, organization, expires_at}`; 403 `invalid_invite` for every bad link |
| `POST /v1/auth/invites/accept` `{token, password, display_name?}` | public: creates the person with the invited role (201 `{user: {id, role, email}}`), no session started; 403 `invalid_invite`, 422, 429 `busy` |
| `POST /v1/auth/me/email` `{new_email, password}` | the signed-in person starts an email change: 202 `{"status":"verification_sent"}` whether or not the address is taken (a notice without a link to the old address is queued in every case; the link to the new address only when it is free, on its own mail queue; all in the background); person only (403 `human_required` for a token); 409 `same_email`, 422, 429 `rate_limited` (5 an hour), 401 `invalid_credentials` (counts as a failed login), 503 `email_not_configured` / `busy` |
| `POST /v1/auth/email/verify` `{token}` | public: finishes the change (200 `{"status":"email_changed"}`); 403 `invalid_verification_token` for every bad link, 409 `already_exists` if someone took the address meanwhile; sessions stay signed in |
| `POST /v1/auth/password-reset/request` `{email}` | public: always 202 `{"status":"accepted"}` (same bytes and headers for every address, known or not); 429 `rate_limited` (5 an hour per client), 422, 503 `email_not_configured` / `busy`; a known verified user with a password gets a mail with `<PUBLIC_URL>/reset-password#t=nmr_...` |
| `POST /v1/auth/password-reset/redeem` `{token, password}` | public: 200 `{"status":"password_changed"}`, every session of the person revoked; 403 `invalid_reset_token` for every bad link; 422; 429 `busy`; 503 `busy` (no mail slot) |
| `POST /v1/auth/users/{user_id}/password-reset` `{}` | owner: 202 `{user, expires_at}` (never a link); revokes the person's sessions and API tokens at once and refuses the old password until a reset is redeemed; 409 `use_self_service` / `user_disabled` / `no_password_credential` / `email_unverified` / `email_unsupported`, 429 after 5 an hour per person, 503 |
| `POST /v1/auth/tokens/revoke-by-link` `{token}` | public: 200 `{"status":"revoked"}` revokes the one API token named by a link from a reset notice mail; 403 `invalid_revoke_link` |

- **Cookies:** `nm_session` (access, 15 minutes) and `nm_refresh` (8 hours idle, 7 days absolute) are `HttpOnly`; `nm_csrf` is readable by the page. All are `SameSite=Strict`, `Path=/`; over https (or behind `--trusted-proxy`) they are `Secure` with the `__Host-` prefix.
- **Refresh grace window (T109):** a refresh token that was just spent and is presented again less than 30 seconds after its use (the second tab of one browser, a retry after a lost response) gets the **same new pair** back: 200, the same cookies, `csrf_token` and body, nothing written, no ledger event. At 30 seconds or later, or once the new refresh token has itself been used, a spent token is reuse: 401 `session_invalid`, cookies cleared and the whole session revoked (`refresh_reuse`). A replay of a session that has ended (logout, admin, erasure, absolute end) is a plain 401. Operators: `NURAMESH_REFRESH_GRACE_SECONDS` (whole seconds 0 to 30, default 30, 0 turns it off; anything else stops the server at start) and, while it is on, `NURAMESH_REFRESH_SUCCESSOR_KEY` (a server secret of 32 or more random bytes as base64url, the same on every replica; without it `serve --auth` refuses to start). **First-run exception:** on a loopback server (`--host` in 127.0.0.0/8, `::1` or `localhost`) with no trusted proxy, no key and `NURAMESH_REFRESH_GRACE_SECONDS` unset, the window is off (0 s) and no key is needed; a window set on purpose, a proxy or a network address still needs the key. With the window off, a refresh token used a second time is reuse at once, so two browser tabs refreshing at the same moment revoke the session (sign in again).
- **Who and where:** login, refresh and `GET /v1/auth/session` answer `user: {id, org_id, role, email, display_name}`; the app files its saved drafts under organization and person (T127).
- **Last owner (T126):** erasing the only active owner is refused with 409 `last_owner` ("transfer ownership to another owner first"). No API or CLI route erases users yet; `AuthService.erase_user` is the one entry point.
- **CSRF:** every mutation made with the cookie session sends `X-CSRF-Token` (the `csrf_token` from login, refresh or `GET /v1/auth/session`, also in the `nm_csrf` cookie). Missing or wrong: 403 `csrf_invalid`. On a 403 the app reads `GET /v1/auth/session` for the current token and retries once.
- **Errors never say which field was wrong**; validation errors on `/v1/auth/*` echo nothing from the request.
- **Headers:** every response carries `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`, `Permissions-Policy`, a Content-Security-Policy, `Cache-Control: no-store` (and HSTS over https). CORS is off: no `Access-Control-*` header is ever sent.
- **First start:** `nuramesh serve` with no owner ever recorded, started from a terminal on loopback (not in a container, not behind a proxy, not hosted), prints ONE line with the setup link `http://<host>:<port>/setup#token=<token>` to the terminal (standard output; never a log, a file or the ledger), once per process. The token is in the fragment, which a browser never sends, and expires 900 seconds after it is issued; starting again makes the earlier link dead. In a container, behind a proxy or without a terminal it creates no token and prints only a hint (no secret), because its output can be a container log even on a pseudo-terminal (`docker run -t`). `nuramesh auth new-setup-token --team <team>` (terminal only; in a container `docker exec -it <container> nuramesh auth new-setup-token --team <team>`) shows a new one once and retires any open one.
- **Proxy:** `nuramesh serve --trusted-proxy` needs `NURAMESH_TRUSTED_PROXIES`, the addresses of your proxies as CIDRs or single addresses separated by commas (for example `10.0.0.0/8,192.0.2.7`). Only when the socket peer is inside that list does the server trust the **last** value of `X-Forwarded-Proto` and of `X-Forwarded-For` (over every header line and comma list: the entry the proxy appended); from any other peer both headers are ignored, so a client that reaches the port directly cannot forge its address or fake `https`. The flag without the list, a list without the flag, an empty or malformed list, and a `/0` network all refuse to start.
- **Lockout:** a login reserves its failure on the client and account counters before hashing, in the lock check's transaction, so concurrent wrong logins cannot overrun the threshold; a success or a `busy` refusal gives it back (a lock then stays only while the remaining count is at or above the threshold). A wrong password settles the failure and writes `auth.login_failed` (and `auth.lockout` when it starts a lock) in one transaction; lockout counters are keyed with the identifier keys and carried over an epoch boundary during the 3600 s window.
- **Ledger and security log:** `auth.login_failed` `{identifier_hash, via}`, `auth.lockout` `{scope, identifier_hash}`, `auth.session_created` `{session_hash}`, `auth.session_revoked` `{session_hash, reason}` (`logout`, `refresh_reuse`, `superseded`, `admin`, `user_erased`) and `auth.setup_token_used` `{}` go to `org:<org>` as minimal facts; identifiers are keyed hashes (a known user's under that user's own key, deleted on erasure; anything else under the organization's key of the current 90-day epoch, destroyed after the 3600 s lockout window). The raw `email` (only when the typed text is a valid address; a typed non-address is never stored), `client_address` and `user_agent` go to the `security_log` table in the same transaction, expire after 90 days and are deleted with their user. Refusals that change nothing write nothing.
- **No docs page beside sessions:** `/v1/docs` (Swagger UI loaded from jsdelivr at an exact pinned version with SRI integrity hashes, under a CSP that allows only those two URLs) is served only by a server started without an organization; wherever the auth endpoints exist it is 404. `/v1/openapi.json` stays.

## Pointing the server at a store (real-data mode)

```bash
# a ledger file from earlier runs (read-only use is safe; the file is the ledger)
uv run nuramesh serve examples/dev-team --store path/to/ledger.db --port 8765

# also check the signed anchors (a Git repo with the anchor files + the public key)
uv run nuramesh serve examples/dev-team --store path/to/ledger.db \
  --anchors <anchor-repo> --pubkey path/to/anchor-signing.pub
# human actions (Stop, approvals) need --auth and a signed-in person; see the sessions section
```

`--store` is a SQLite file (default `<team>/.nuramesh/runs.db`, the file `nuramesh try` writes, so a run started in another terminal shows up live). With `NURAMESH_DATABASE_URL` set the server uses PostgreSQL instead; every endpoint below goes through the `Store` interface and works on both. The team folder (`examples/dev-team`) only supplies the organization for reports and eligibility (its id is `devteam-example`; the historical bundles were recorded under the old id `demo`, which key tombstones no longer allow in shipped examples, so their reports show that id); run, ledger, spend and verify endpoints work from the store alone.

## Endpoints added or extended

| Method and path | What it returns |
| --- | --- |
| `GET /v1/runs?limit=50&status=` | `{"runs": [...], "truncated": false, "scanned": 2}` (`scanned` counts the caller's own runs looked at): runs, most recently active first (`limit` 1 to 200), as run summary rows (below). `status` must be one of the values below (else 422). The work per request is bounded (500 runs, 50,000 ledger entries looked at; a finished run is classified from its last entry without reading its stream); `truncated: true` means a bound stopped the search before it was complete. |
| `GET /v1/runs/{id}` | Run detail: summary plus tasks (gates, verdicts, attempts, spend), spend by agent and model, run budgets, approvals, ledger status. |
| `GET /v1/runs/{id}/ledger?after_seq=&limit=&type=&task_id=` | The run's ledger stream, paged (limit 1 to 500). `type` is a prefix (`gate.`, `budget.`, `run.`). |
| `GET /v1/runs/{id}/ledger/verify` | Chain check of this run's stream and, if configured, its anchors. Counts against the caller's verify budget (below). |
| `GET /v1/ledger` | Table of contents: every stream with its head. |
| `GET /v1/ledger/verify?limit=50&after=` | Verify streams in name order, at most 100 per request; while `truncated` is true continue with `after=<next_after>` (`next_after` is null exactly when `truncated` is false; it is the last stream shown, or the `after` you sent when the page shows none, and a page with no streams can be `truncated`: keep asking, each request looks up further streams). **Verify budget (TM-10):** every verify route (`/v1/ledger/verify`, `/v1/runs/{id}/ledger/verify`, `/v1/ledger/{stream}/verify`) counts against one budget per caller: a signed-in session, API token or agent credential has 600 calls a minute, keyed by that credential (not by address); a caller that is not signed in (authentication-off local mode) has 60 per minute per client; over it, 429 `rate_limited` with `Retry-After`. **In flight:** at most 8 verifications run at once per server process, and at most 2 at once per holder of them; a further one answers 429 `busy` (`Retry-After: 1`) at once, before the run or stream lookup and before any stream is read, so the answer is the same for every id. The holder is a stable identity, never the session: an API token, an agent (not its credential), the person behind a session (signing in again gives no second cap), or the client's /64 network for a caller that is not signed in. Both refusals are the same `busy`; retry after 1 second. |
| `GET /v1/ledger/{stream}` , `/verify` | As before (`head` added to the page; `anchor_status` to verify). |
| `POST /v1/runs/{id}/stop` | Human only. Asks the run to stop (below). |
| `GET /v1/runs/{id}/stop` | Stop state: `requested`, `requested_by`, `ended`, `stopped`. |
| `POST /v1/runs/{id}/tasks/{task}/gates/{gate}/decision` | Human session only (`decide`). With authentication on (the default): 401 `authentication_required` with no credentials, 403 `human_required` for an API token, both before the content type or body is read; strict body (nothing coerced, no client text echoed in a 422). With `--auth off`: 403 `authentication_required`. A person decides a human gate a durable run waits on: `{attempt, submission, findings: {criterion: {approved, reason}}}`; 201 first time, 200 for an identical replay; refusals are problem codes (the internal specification, T81); `attempt` N and `submission` M are the task's own counters; another organization's run is 404 `not_found`, the same as a missing one. The answer never contains the text. |
| `GET /v1/approvals/{id}` | One approval with the ledger entries that name it. |
| `GET /v1/budgets` , `GET /v1/budgets/{id}` | The caller's organization's budgets only (a budget belongs to the organization whose ledger holds its earliest `budget.defined` entry, found through an index of those entries so no amount of other ledger entries can hide it from its owner; another organization's, or a missing one, is 404 `no such budget`, and `POST /v1/budgets/{id}/unblock` answers the same and changes nothing). Balances in their **current window** (money as `{amount, currency}`), `window` (`none`, `day`, `month`), `on_breach` (`reset` or `hold`, T56), `window_start`, `available`, the stored `definition`, and for one budget its ledger history (reads at most 20,000 organization-stream entries; `history_truncated` says if it was cut). |
| `GET /v1/reports/downgrade?min_sample=30` | The evidence-based downgrade report (R1.34), read-only and scoped to the server's organization: per role, the evidence for each model next to its cost per accepted result, and one `recommendation` for a cheaper model only where the evidence supports it (below). Needs an organization (409 `no_config` without). |
| `WS /v1/runs/{id}/events?after_seq=N` | Live ledger of one run (below). |
| `WS /v1/events` | Live ledger of every stream (subscribe message, below). |

**Limits and replicas.** (a) The verify caps and budgets are **per process**: with N API replicas the effective budget and slots are N times larger, and one credential can run `VERIFY_HOLDER_IN_FLIGHT` (2) x N verifications at once across the fleet; shared limits need a shared store and are not built. (b) The request counters are an LRU of `MAX_TRACKED_CLIENTS` (10,000) keys: an unauthenticated caller using more than 10,000 distinct /64 networks can evict counters and so reset windows, including its own. This applies to the unauthenticated limits only (`/v1/health`, and the verify routes in authentication-off local mode); signed-in credential budgets are not reachable by an unauthenticated caller (a signed-in caller who creates many sessions can still push its own session keys into the same LRU and reset its own budget windows; the in-flight cap is not affected, it is keyed by the person). The in-flight table is not an LRU: it holds only holders with a verification running. (c) The cap limits one holder, not a crowd: many credentials, or many /64 networks, can still fill the pool of 8; that is what the pool is for (it protects the server's threads, never any single caller's share). (d) The proxy trust list must contain only the operator's own proxies: a too wide list lets a client set `X-Forwarded-For` and choose its own anonymous key; a wrong list collapses all anonymous callers into one key (and one cap of 2).

### Models (`/v1/models/*`)

Catalog, assignments and connections are ledger state on the organization stream; the configuration only seeds the catalog. A server without an organization answers 409 `no_config`. Answers carry `Cache-Control: no-store`. Every write body rejects unknown fields and (except connection writes, where it is optional unless a repoint) carries `reason` (3 to 500 characters, kept in the content store, only its hash in the ledger). Every POST and the PUT need a signed-in `owner` session with the CSRF token (`X-CSRF-Token`); an API token gets 403 `human_required` on any admin route, GET included; a lower role gets 403 `forbidden`. Writes answer 201 when they create a record (an entry, a price version, an assignment event, a first connection) and 200 otherwise; refusals are 404 `not_found`, 409 (`already_exists`, `retired`, `not_paused`, `in_use`, `price_version_exists`), 422 `invalid_request` with `problems: [{path, message}]`, 503 `content_unavailable`.

| Route | Permission | Does |
| --- | --- | --- |
| `GET /v1/models/catalog` | read | `{seeded, config_drift, ignored_events, customer_regions, entries: [{provider, model, family, processing, status, data_classes, price, connected, origin, updated_by, updated_at}]}` |
| `GET /v1/models/catalog/events` | admin | a page of the honored catalog and assignment events: `{events, next_before}`. The `limit` newest honored events (query `limit` 1 to 1000, default 500) with `seq < before` (query `before`, 0 to 2^62, absent means from the newest), in ascending `seq` within the page; `next_before` is the `seq` of the first row of the page when an older honored event exists, else null (pass it as `before` for the next older page). Ignored events never count toward `limit`. A value outside the range is 422 `invalid_request` at `$.query.limit` or `$.query.before`. Each row has its reason (or null once expired, unreadable, or not a reason). Each reason text shown writes one `content.read` entry (`{content, why: "api"}`, never the text), so one GET writes at most `limit` entries |
| `POST /v1/models/catalog/entries`, `/edit`, `/approve`, `/pause`, `/retire`, `/remove` | admin | body `provider, model, reason` (add also `family, processing`; edit optional `family, processing`); pause and retire answer `affected: [...]`; a widening edit answers `repaused: true` |
| `POST /v1/models/catalog/prices` | admin | a new price version (decimal strings, `effective_from`, `source`); a duplicate `(provider, model, effective_from)` is 409 `price_version_exists` |
| `POST /v1/models/catalog/regions` | admin | `customer_regions`; always 200; `repaused: [...]` lists approved entries paused again |
| `GET /v1/models/assignments` | read | `{"assignments": [{subject, scope, id, role, ref, level, source}, ...]}` (an object, not a bare array) |
| `PUT /v1/models/assignments/{scope}/{subject}` | admin | `model` (or null to clear), `reason`; always 200 (R1.43 A85) |
| `POST /v1/models/assignments` | admin | deprecated, use the PUT: `scope, id, model` (or null to clear), `reason` |
| `GET /v1/models/connections` | admin | the keyless connections (no key or secret field exists) |
| `GET /v1/models/connections/events` | admin | the change history of the connections, newest page first: `{rows, next_before}`. Query: `provider` (a lower-case slug), `endpoint_id` (one `openai-compatible` connection), `before` (0 to 2^62; absent means from the newest), `limit` (1 to 200, default 50); a value outside the range is 422 `invalid_request` at `$.query.<name>` and is never echoed. `rows` are the `limit` newest matching entries with `seq < before`, ascending within the page; `next_before` is the `seq` of the first row when an older matching entry exists, else null. A row has `seq, type, actor, at, provider, valid`; a created or updated row also `endpoint, status, auth, repoint` (true when the entry changed where the connection points), `reason` (text or null) and, for `openai-compatible`, `endpoint_id` and `key` (a key NAME); a removed row `reason` and, for `openai-compatible`, `endpoint_id`; a tested row `ok, code, status_class` and, for `openai-compatible`, `endpoint_id`. A stored-key entry is a row of its connection with `key_id` and `version` (a number, or `"all"` on a revoke of every version): `model.key_stored`; `model.key_activated` also `previous_version` (null on the first key), `grace_seconds` and `reason`; `model.key_test_failed` and `model.key_read_failed` also `code` (a fixed failure code); `model.key_revoked` also `cause` (`connection_removed` or null) and `reason`; `model.key_destroyed` also `cause` (`revoked`, `expired`, `failed`, `abandoned`, `orphan`, `unreferenced`, `rotation_lost`), shown on the connection whose key entry first named the key id (a destroy of a key no connection stored, and the organization-wide erasure records, are no row). A key row never holds the fingerprint, its hash markers, `via` or `mfa`. A row the resolver would refuse (a malformed or hand-written entry, an unsupported provider or pair, a host the adapter does not allow) is `valid: false` with `provider` `"(invalid)"`, `reason` null and no other ledger value; it matches no filter. On a hosted server (or with an invalid hosted flag) every `openai-compatible` row of any type, removed and tested ones too, is such a placeholder. Each reason text shown writes one `content.read` entry, so a GET writes at most `limit` entries and nothing else. A read role or an API token gets the same refusal as `GET /v1/models/connections` |
| `PUT /v1/models/connections/{provider}` | admin | `endpoint`, `status` (`active` or `disabled`), `reason` (required for a repoint, which answers `repaused: [refs]`) |
| `POST /v1/models/connections/{provider}/remove`, `/test` | admin | remove (a connection that holds a stored key has that key revoked in the same step, ledger `model.key_revoked` with cause `connection_removed`, and the value is destroyed by the sweeper); or one bounded metadata request (10 s, rate limited, refused while the org is halted) |
| `PUT /v1/models/connections/{provider}/key` | owner | stored model key: set the first key or rotate it. Body `{value, grace_seconds?, reason?}` (`value` is write-only and never returned; `reason` 3 to 500 characters is required for the first key). Stored keys are for `anthropic` only: any key operation (PUT, DELETE, GET) on an `openai-compatible` connection is 422 `invalid_request` ("stored keys are supported for anthropic only; openai-compatible keeps its configured credential") and changes nothing. 201 for a first key, 200 for a rotate: `{key_id, version, state, created_at}`, no fingerprint. The new version is tested with one provider request first; a failed test is 422 `key_test_failed` with `failure_code` (auth_failed, not_found, redirected, server_error, unreachable, refused, test_error) and the connection is unchanged. Refusals (fixed texts, the body is never echoed): 403 `forbidden` (not an owner session), `not_allowed_yet`, `mfa_unavailable`, `mfa_enrollment_required`, `mfa_required` (hosted rules, spec r114c 7a), 404 `not_found`, 409 `rotation_in_progress`, `version_conflict`, `org_erased`, `org_key_unavailable`, 422 `key_shape`, `invalid_grace`, `reason_required`, 503 `secret_store_unavailable`. Not idempotency-keyed (the body holds a key) |
| `DELETE /v1/models/connections/{provider}/key` | owner | revoke one version (`version`) or all; body `{version?, reason}` (reason required). 200 `{key_id, revoked_versions}`; revoking what is revoked writes nothing |
| `GET /v1/models/connections/{provider}/key` | read | the versions and their states; with the admin permission also `key_id`, `created_at`, `activated_at`, `grace_until`; 404 `unknown_key` |
| `GET /v1/models/keys/health` | admin | `{"store": "ok" or "unavailable"}` |
| `GET /v1/models/resolution?data_class=` | admin | `{ok, blocks, resolutions}`: what a run of the organization's agents would resolve; always 200, writes nothing |

Anything that starts a run answers a start refusal with 409 `model_blocked` and `blocks: [{agent, role, provider, model, reason, detail}]`. The app's recorded answers for all of these are `apps/app/src/test/contract/models.json` (regenerated by an internal script, checked by an internal test).

- **Database busy (any route).** While the store cannot take a request in time, any `/v1` route (and the auth middleware before it) can answer 503 `store_busy` ("the database is busy; retry"); nothing was changed, retry after a moment. It is global, so the per-route tables do not repeat it.
- **`Idempotency-Key`** (R1.35). Send it on a mutating route that honors it (the model catalog and connection admin, approvals and gate decisions, budget unblock, run stop, token revoke, the kill switch and the breaker routes, agent-credential revoke and revoke-all; revoking always works, even for an organization whose key was destroyed, and then runs as if no key was sent); a repeat with the same key by the same person and organization answers the first 2xx answer byte for byte with `Idempotent-Replayed: true` and runs nothing again. A key is 16 to 128 characters (letters, digits, `.`, `_`, `-`); the same key with a different request is 422 `idempotency_key_reused`; a request still running is 409 `idempotency_in_progress` (retry after a moment; a lease lapses after 120 s). Only a 2xx is stored (24 hours), sealed with the organization's content key; every refusal and every 5xx releases the key. Routes that only read or carry a secret (login, refresh, token and agent-credential creation and rotation) answer 400 `idempotency_key_unsupported`. A keyed request needs the `content` extra and the key ring: without them it is 503 `idempotency_unavailable` with `Retry-After: 5` and the detail below, and the route is not run (except the three revoke routes, which run as if no key was sent when the ring cannot be read); an unkeyed request is never affected, and the CLI and the app send no key on halt or role pause.
  - No key ring: `Idempotency keys are unavailable on this server: the content key ring is missing or cannot be read. The request was not run. Send it again later, or without an Idempotency-Key header, and ask the operator to install the content extra and configure the key ring.`
  - Store unreachable: `Idempotency keys are unavailable on this server: the idempotency store cannot be reached. The request was not run. Send it again later, or without an Idempotency-Key header.`
  - Answer could not be stored after the route ran (at-least-once routes only): `Idempotency keys are unavailable on this server: the answer to this request could not be stored. The request may have been carried out. Check the current state before you send it again; do not assume it did not run.`

All money is `{"amount": "0.00052050", "currency": "USD"}`: decimal text, never a JSON number. Times are ISO 8601 UTC. An unknown price is never guessed: that call has no cost and counts in `unknown_cost_calls`.

## Run summary (`GET /v1/runs`, and the top of the detail)

Real example (run `run-20261023-193348-ef9e`, the first real run, two attempts, then a human was needed):

```json
{
  "run_id": "run-20261023-193348-ef9e",
  "status": "needs_human",
  "org": "demo",
  "started_at": "2026-10-23T19:33:48.295324+00:00",
  "started_by": "system",
  "ended_at": "2026-10-23T19:33:52.180082+00:00",
  "tasks": {"total": 3, "by_status": {"cancelled": 2, "needs_human": 1}},
  "gates": {"total": 2, "passed": 1, "failed": 1, "error": 0},
  "spend": {"total": {"amount": "0.00109500", "currency": "USD"}, "calls": 2, "unknown_cost_calls": 0},
  "stop": null,
  "events": 32,
  "head": "7356616571f543841ffa0c1e607064890f029c14be8c1a6438a4482f1c761d77",
  "updated": "2026-10-23T19:33:52.180082+00:00"
}
```

- `status`: `running`, `stopping` (a stop was requested, the run has not ended), `stopped`, `done` (all tasks done), `needs_human` (ended with a task waiting for a human: `needs_human`, `blocked` or `verifying`; runs cannot resume before R1.20), `failed`, `aborted` (the engine itself failed). The app's `RunStatus` words map as: running/stopping to running, needs_human to waiting, aborted to failed.
- `gates` counts the **latest recorded outcome of each gate** of each task. `events` and `head` are the stream's last sequence number and hash; use `events` as the cursor for the live view.
- The other real run, `run-20261023-193432-f5e5`, is `done` (3 tasks, 5 gates passed, spend `0.00109650`).

## Run detail (`GET /v1/runs/{id}`)

The summary plus:

```json
{
  "chief_of_staff": "chief", "canon_commit": null,
  "ledger": {"stream": "run:run-...", "seq": 32, "head": "7356...", "ok": true},
  "tasks": [{
    "id": "run-20261023-193348-ef9e-fix-add", "title": "Fix the failing add() test in calc.py",
    "assignee": "dana", "status": "needs_human", "attempt": 2, "max_attempts": 2, "submission": 2,
    "note": "attempts used up; failed: tests",
    "definition_of_done": {"criteria": [{"id": "fixed", "text": "..."}], "gates": [{"id": "tests", "kind": "test_command", "required": true, "checks": ["tests-pass"]}]},
    "attempts": [{
      "attempt": 1, "submission": 1,
      "gates": [{
        "gate": "tests", "kind": "test_command", "required": true, "outcome": "failed",
        "checked_by": "gate:test_command", "result_hash": "a3d02124...", "ledger_seq": 17, "at": "...",
        "verdicts": [{"criterion": "tests-pass", "text": "The project's tests pass in the sandbox", "verdict": "not_met"}]
      }],
      "moved_to": "in_progress", "reason": "a required gate failed; another attempt", "verification": [{"gate": "tests", "required": true, "state": "failed", "deciding_results": ["a3d0..."]}]
    }],
    "latest_gates": [ ... the last attempt's gates ... ],
    "spend": {"total": {"amount": "0.00109500", "currency": "USD"}, "calls": 2, "unknown_cost_calls": 0}
  }],
  "spend": {"total": {...}, "calls": 2, "unknown_cost_calls": 0,
            "by_agent": [{"agent": "dana", "total": {...}, "calls": 2, "unknown_cost_calls": 0}],
            "by_model": [{"model": "azure-openai/gpt-5.4-mini", "total": {...}, "calls": 2, "unknown_cost_calls": 0}]},
  "budgets": [{"id": "rb-9c2aeb60cd4c", "scope": "run", "scope_id": "run-...", "limit": {"amount": "2.00", "currency": "USD"},
               "spent": {...}, "reserved": {...}, "available": {...}, "blocked": false, "definition": {...}, "version": 7}],
  "budget_alerts": [{"kind": "hard_stop", "ledger_seq": 11, "budget": "...", "scope": "run", "window": null, "limit": {...}, "resets_at": null, "held": false}],
  "approvals": [], "stop": null
}
```

- **Verdicts are the verifier evidence the ledger keeps:** per criterion `met` or `not_met`, with the criterion's text from the task's definition of done, who checked (`checked_by`: `gate:rule`, `gate:test_command`, `agent:<verifier>`, `human:<id>`) and `result_hash` (the hash of the full recorded result). `result_hash` is a keyed hash (HMAC-SHA256 under the organization's key) when `hash_scheme` is `hmac-v1` (with `hash_key_id`, the public key fingerprint); a gate recorded before keyed hashes has `hash_scheme: null` and a plain SHA-256. The verifier's free-text quotes are **not** in the ledger so they cannot be shown.
- `budget_alerts` are the run's budget hard stops (`hard_stop`) and overspends (`limit_passed`) as recorded in the ledger, with the budget's window and when it resets or that a human must lift it: the data behind the CLI's `BREACH ALERT` lines. Empty when the run hit none.
- `budgets` are the budgets this run spent from or could spend from. `definition` is the stored budget exactly as the engine models it, so a field a newer engine adds (for example a time window) appears there without an API change; the fixed keys (`limit`, `spent`, `reserved`, `available`, `blocked`) are stable. The app should read windows from `definition` and treat unknown keys as optional.
- `tasks[].attempts[].moved_to` and `reason` are the engine's recorded decision after the round (`done`, `in_progress` for another attempt, `needs_human`), not something this API works out.
- A run that has not ended returns the live numbers; poll the detail or use the live view and re-fetch on each event.

## Ledger page

`GET /v1/runs/{id}/ledger?type=gate.&limit=2` returns the stored events themselves (hashes, no content):

```json
{"stream": "run:run-20261023-193348-ef9e",
 "events": [{"seq": 16, "type": "gate.completed", "actor": "system", "at": "...", "run_id": "...", "task_id": "...",
             "payload": {"attempt": 1, "checked_by": "gate:rule", "gate": "has-fix", "outcome": "passed",
                         "result_hash": "8766e699...", "submission": 1, "verdicts": [{"criterion": "fixed", "verdict": "met"}]},
             "cost": null, "cost_basis": null, "prev_hash": "26fd7588...", "hash": "7dda526e..."}],
 "head_seq": 32, "head": "7356...", "next_after_seq": 17, "has_more": true}
```

**A person's text is not shown (R1.27-H).** The routes that return ledger entries (the run and stream pages, the task timeline, both websockets, the budget history) set a `reason` or `note` payload value to `null` unless the entry's actor is exactly `system`, and then adds `"redacted": ["reason"]` (the names of the keys it hid) beside the payload; `hash` and `prev_hash` stay as stored, so a client cannot recompute `hash` from such an answer. A `decision.recorded` entry of kind `engine_incident` has no `note` for any actor and carries `note_present` (true or false). In the run view a task's `note` and an attempt's `reason` are `null` when `system` did not write them (they were `""` or text); in `GET /v1/runs/{id}/report` the task `note` is `""` then; an approval's `decision_reason` is always `null` (`decision_reason_hash` stays). `GET /v1/models/catalog/events` (admin only) still shows the text behind a catalog entry's `reason_hash`, but writes one `content.read` ledger entry (who and which hash, never the text) for every reason it shows, as the reveal route does (R1.27-H H3). The ledger itself, the CLI and exports are unchanged.

Page with `after_seq=next_after_seq` until `has_more` is false. With a filter, `next_after_seq` is the last entry **read** (not the last returned), so paging never skips or repeats; one request reads at most 5,000 entries.

## Verify

`GET /v1/ledger/verify` (or `/v1/runs/{id}/ledger/verify`):

```json
{"ok": true, "chain_ok": true,
 "streams": [{"stream": "run:run-20261023-193348-ef9e", "ok": true, "checked": 32, "problem": null, "at_seq": null,
              "head": "7356...", "anchors": {"anchored": true, "count": 1, "unanchored_events": 0, "problems": []}}],
 "anchors": {"configured": true, "checked": true},
 "note": "chains are checked against the database's own head; anchors prove history"}
```

- `chain_ok`: every stream's hash chain verifies against the database's own head (accidental damage and edits inside the chain). `ok` also requires the anchors to check out when they are configured.
- **`ok` is not "anchored".** `ok` means nothing wrong was found. `verified` (top level and on the single-stream endpoint) is true only when every stream's chain is intact **and** each has signed anchors that check out. A stream with no anchors is `ok: true`, `verified: false`, `anchors.status: "unanchored"` (`anchors.anchored: false`); the other statuses are `anchored` and `problems`. The app should show "verified" only when `verified` is true, and "not anchored yet" otherwise.
- Only the streams asked for are read (the anchors of other streams in the repository are ignored).
- `anchors.configured` is true when the server has `--anchors` and `--pubkey`. `anchors.checked: false` with an `error` means the anchors could not be read: that is **not** clean, and `ok` is false on **every** verify endpoint (the per-stream ones too). The error text is fixed and never contains server paths (details go to the server log). `streams[].anchors` is null when anchors are not configured.
- `unanchored_events` counts entries newer than the last signed anchor (they are reported, not hidden).

## Task audit (`GET /v1/runs/{run_id}/tasks/{task_id}/audit`, R1.27; spec: the internal specification)

Read-only, permission `read`, closed to agent credentials (`403 agent_forbidden`, before any lookup). Everything that happened to one task, from one snapshot of the run's ledger: `task` (the run page's task without `attempts`, `latest_gates`, `spend` and `note`), `chain` (`ok`, `head_seq`, `head`), `entries` (the same objects as `GET /v1/runs/{id}/ledger?task_id=`, oldest first, at most 1000: `truncated` and `total_entries` say more exist), and sections computed from all of the task's entries: `attempts` (the run page's rounds plus `reason_hash`), `spend` (`total`, `calls`, `unknown_cost_calls`, and its split `price_unknown_calls`, `other_currency_calls`, `failed_calls_no_cost`), `spend_by_attempt` (every attempt from 0 up; a call belongs to the attempt current when its entry was written), `approvals` (legacy `decision_reason` is always `null`), `decisions` (one row per `decision.recorded`, by kind; an `engine_incident` has only `note_present`) and `rework` (`by` is `gate`, `person` or `other`). Reason text appears only for actor `system` (R1.27-H rule); `reason_hash` and `feedback_hash` always. Errors: 404 `not_found` (`no such run` or `no such task`, the same bytes for a foreign or missing id). It writes nothing (no `content.read`).

## Live view

- **Per run:** `WS /v1/runs/{id}/events?after_seq=N`. No subscribe message. The server first sends every entry after `N` as `{"stream": "run:...", "event": {<ledger event>}}`, then `{"type": "caught_up", "cursor": {"run:...": 32}}`, then each new entry as it is appended. Reconnect with the last `seq` you saw: nothing is missed and nothing repeats. Closes with code 4404 if the run has no stream. With `--auth` the credential is checked again every tick; when it stops working (expired session, revoked token, logout, erased user) the socket closes with **4401** and nothing more is sent. 4401 means refresh or sign in, then reconnect with the last `seq`; 1008, 1003 and 4404 are final. The app renews the session once (shared with its REST calls and other tabs) and reconnects from the last `seq`; if the renewal is refused it stops and shows the login. Each stream re-reads the store every tick for this check (2 reads with a token, 3 with a session), under the `--max-live` cap.
- **Everything:** `WS /v1/events`; send one JSON message first (within 5 s): `{"streams": ["*"], "cursor": {"run:r1": 12}}`.
- Entries appended by **another process** (the CLI writing the same SQLite file) arrive too: the server polls the store's stream heads (one query per tick, at most 500 entries per tick). The interval is `--poll-ms` (default 500, 10 to 10000); p95 latency stays under the 1 s target.
- At most 50 live connections at once (`--max-live`); one more is closed with code 1013 ("too many live connections; try again later").
- Browsers send an `Origin`; only loopback origins on the server's own port are accepted (cross-site hijacking is closed).

## Start a run (`POST /v1/runs`, R1.20; spec: the internal specification)

A person signed in starts a run: `{"tasks": [{"ref", "role", "depends_on": [refs], "title", "definition_of_done"} or {"ref", "role", "depends_on", "template": <id of an org task template>}], "data_class": "synthetic" or "customer"}`. The body is strict (an unknown field, a client `org_id` or a client `run_id` is 422; at most 64 KiB and 50 tasks). `data_class` has no default; `customer` answers 422 `customer_data_not_enabled` until `NURAMESH_CUSTOMER_DATA_ENABLED=1`. Task ids are assigned by the server (`<run_id>-t<n>`). **API tokens of any scope and agent credentials are refused** (403 `human_required` / `agent_forbidden`).

The start is **asynchronous**: the answer is `202 {"run_id", "state": "accepted" or "already_requested", "engine"}` with `Location: /v1/runs/{run_id}`. The API never runs team code. A runner (the light engine's `nuramesh worker --engine light`; the Temporal engine's intake is a separate step) takes the request, checks the plan against the team's code (unknown role, delegation, verifier, real-model plans, per-assignee budgets) and begins the run, or refuses it: read `GET /v1/runs/{run_id}` (`state: "refused"` with a fixed `refusal_code`, and one `decision.recorded` entry `run_start_refused` on the organization stream). Titles and definitions of done are stored in the content store (kind `plan_text`, one row per run and text, tagged with the people they name) and never in the ledger or the tasks table; the ledger holds ids and keyed hashes.

The run id comes from the `Idempotency-Key` when there is one: it is derived from (organization, actor, route, key), so the same key, actor and body is the same run (a replay carries `Idempotent-Replayed: true`; after the 24 h key expiry the same body answers `already_requested`, another body or another `data_class` 409 `run_id_conflict`). Without a key a retry is a new run. Order of checks: authentication (401), person only (403), idempotency key (400/422), content type, size, JSON and schema (415/413/400/422, `customer_data_not_enabled`), then the organization's rate (`NURAMESH_RUN_START_RATE`, default 60 a minute: 429 `run_rate_limited`) and cap (`NURAMESH_MAX_ACTIVE_RUNS`, default 50 unfinished runs plus pending requests: 429 `run_cap_reached`, both with `Retry-After`; a 429 writes no ledger entry), the organization's state (409 `org_halted` with the usual halt record, `org_erased`, `org_key_destroyed`, `org_key_missing`), the plan's shape (422 `plan_cycle`, `plan_too_many_tasks`, `plan_invalid`, `template_unknown`; one `run_start_refused` entry), the engine (503 `engine_unavailable` before any write) and a read of the organization's budgets (409 `run_budget_missing`, `run_budget_blocked`, `run_budget_exhausted`; one `run_start_refused` entry). Nothing is reserved at the start.

`GET /v1/health` adds `engine` (`light` or `temporal`) and, for the light engine, `light_engine`: `active` (a runner holds the lease) or `absent` (no runner: requests wait, up to `NURAMESH_RUN_START_TTL_SECONDS`). For the Temporal engine it adds `starter`: `{reachable, namespace, task_queue}`; `reachable: false` means the starter's own health check failed or raised (the Temporal server could not be reached), so new runs cannot be started until it recovers; `namespace` and `task_queue` say where runs are sent. The engine is chosen by `NURAMESH_RUN_ENGINE` (`auto`, `light`, `temporal`; `auto` picks Temporal when `NURAMESH_TEMPORAL_ADDRESS` is set, else the light engine). A hosted deployment (`NURAMESH_HOSTED=1`) runs Temporal only: `nuramesh serve` exits 2 before binding a port with the light engine, without an address, or with `NURAMESH_TEMPORAL_ALLOW_PLAINTEXT=1`.

## Stop

`POST /v1/runs/{id}/stop` with `{}` or `{"reason": "text"}` (the body allows nothing else; the actor is the signed-in person or the API token's service actor; with `--auth off` it is refused).

```json
{"run_id": "stop-run", "status": "stopping", "already_requested": false,
 "requested_by": "human:ann", "requested_at": "2026-10-23T20:01:02.123456+00:00", "ledger_seq": 14, "via": "api"}
```

- It appends **`run.stop_requested`** to the run's stream (`payload: {"via": "api", "reason_hash": "<keyed hash>"}`; the reason text goes to the content store, T47, and needs it configured: 503 `content_store_unavailable` otherwise, with nothing written).
- **Stop always works (R1.37).** If the organization's content key was destroyed (an erasure), is missing, or its tombstone cannot be read, the stop is still recorded with 2xx but the reason is not kept: the entry has no `reason_hash`, and the answer, the run view and `nuramesh status RUN_ID` carry `reason_withheld` (`org_key_destroyed`, `org_key_missing` or `key_tombstone_unavailable`) and `stop_reason_withheld` (the text "reason withheld: org key destroyed", "... org key missing" or "... org key unavailable"). The reason text is nowhere.

- **What the engine does** (cooperative: the API writes a request, the engine in whatever process runs the run reads it). It looks for the request at these **checkpoints**: (1) before each task starts, (2) before each attempt of a task, (3) before each worker model call (nothing is reserved if it stops there), (4) before each gate runs, (5) after each gate's result is recorded, (6) after the worker returns, and (7) every 250 ms while tasks run (this only fires the stop hooks, so a long sandbox command is killed). Verifier calls are covered by checkpoint 4: a verifier gate makes exactly one model call, straight after it. When it finds the request it cancels running and waiting tasks with the reason `cancelled: the run was stopped by a human`, makes no further model call, releases reservations still open, kills a running sandbox command or container, and writes `run.ended` with `"status": "stopped"` and `"stop_requested_by"`.
- **A model call that is IN FLIGHT when Stop is requested** finishes. The engine cannot cancel a provider call, so the call completes, is **charged and recorded** (`budget.settled`, `model.called`) exactly as usual, and the stop takes effect at the next checkpoint. If that call was a verifier's, the gate result is recorded too (the money was spent, so the outcome is in the ledger), and then the task is cancelled; a cancelled task never becomes done. So "stopping" can last up to one provider call plus the current step. **The app should show "Stopping..." (run status `stopping`) until the run ends** (`stopped`, or `done` if nothing was left to skip), then show the final spend, which can include one last call made after the click.
- **Ctrl-C** in the terminal that runs `nuramesh try`: the first one (in the scheduling loop, where the engine spends nearly all its time) records `run.stop_requested` (`via: ctrl_c`) and the run stops as above; the actor is `--human` if given, otherwise **`service:cli`** (clearly not a person). A **second** Ctrl-C aborts: it stops waiting for workers that don't return, kills sandbox commands, cancels unfinished tasks and writes `run.ended` with `aborted: true` and the counts of **every** task; the CLI exits with code 130. **One rule: an abort wins over a stop in the status** (`aborted`, never `stopped`, even if a stop was requested first). Limits, stated honestly: (1) an interrupt that lands outside the scheduling loop (while the run is starting or finishing) is an abort, not a clean stop; (2) after an abort **reservations held by a call still in flight are left open on purpose** (the call may still settle them; the reconciler closes what remains after its age limit, and the CLI's `os._exit` may end the process before that, so such a call's cost can be recorded later or, if the process is gone, not at all; the reservation then holds budget until the reconciler runs); (3) in library use a worker thread that is still running may append an entry **after** `run.ended`; "no entry after `run.ended`" is guaranteed for Stop requests, not for a worker that was abandoned by an abort.
- `nuramesh stop` makes the same organization check (exit 3 on a mismatch). The run's organization is read from its own `run.started`: through the API a run of another organization is 404 `no such run`, exactly as a run that does not exist (no 409 `wrong_organization` any more, see "Organization scope"); a server without an organization can still stop runs (a reason is kept under the run's organization).
- **Idempotent:** a second request while the run is going returns the first (`already_requested: true`) and writes nothing. **Finished runs:** 409 `run_finished`. Unknown run: 404. No human configured: 403 `authentication_required`.
- The run status is `stopping` until `run.ended` is written, then `stopped`. A run whose process died without ending keeps `stopping` (nothing is there to honour the request).
- Also available from a terminal: `nuramesh stop ID --team <team> --human human:you [--reason "..."] [--store FILE]` writes the same event; Ctrl-C in the terminal that runs `nuramesh try` records a `run.stop_requested` too (see above). A stopped `nuramesh try` prints the report, then `run <id> was STOPPED by <who>` on stderr, and exits with code 4.

## Organization halt, role pause and breakers (E19; spec: the internal specification)

Nine routes, all acting at the server level: they act on the caller's own organization, and the roles and breakers they name are those of the server's configuration (a server without an organization answers 409 `no_config`). Times are ISO 8601 UTC with `+00:00`. Every POST needs `X-CSRF-Token` with a cookie session.

| Method and path | Perm | Who | Result |
| --- | --- | --- | --- |
| `GET /v1/org/halt` | read | any signed-in caller | `halt` object |
| `POST /v1/org/halt` | decide | a person only | `halt` object plus `stopped_runs`; 409 `already_halted` |
| `POST /v1/org/resume` | decide | a person only | `halt` object; 409 `already_running` |
| `PUT /v1/org/prompt-cache` | admin | an organization administrator (a session; a token never has admin) | body `{"enabled": bool}`; `{organization, prompt_cache: {enabled, changed, ledger_seq, provider_retention}}`; one `decision.recorded` entry per change, none on a repeat; 403 `hosted_refused` in hosted mode; 409 `org_erased` |
| `POST /v1/org/roles/{role}/pause` | decide | a person only | `halt` object plus `stopped_tasks`; 409 `already_paused` |
| `POST /v1/org/roles/{role}/resume` | decide | a person only | `halt` object; 409 `not_paused` |
| `GET /v1/org/breakers` | read | any signed-in caller | breakers object (below); writes nothing |
| `POST /v1/org/breakers/{name}/reset` | decide | a person only | `{"name", "state": "ok", "reset_at", "ledger_seq", "org_halted"}`; 409 `not_tripped` |
| `POST /v1/org/breakers/{name}/disable` | decide | a person only | body `{"reason": "false_positive" or "planned_work" or "rule_being_replaced" or "other"}`; 409 `already_disabled`, 409 `breaker_tripped` |
| `POST /v1/org/breakers/{name}/enable` | decide | a person only | no body; 409 `not_disabled` |

- **The app sends no idempotency key.** Repeating a POST is safe: the state check answers 409 and writes nothing. The app treats such a 409 as "already done" and refetches. A resume is never refused because of the tick: it works at once.
- **`halt` object:** `{"halted", "by", "at", "cause", "rule", "ledger_seq", "phase", "error_class", "failing_rule", "failed_ticks", "roles", "can_act"}`. `cause` is `human`, `breaker` or `evaluation_unavailable` while halted (else null); `phase`, `error_class`, `failing_rule` and `failed_ticks` are set only for `evaluation_unavailable` (the fail-closed halt of the tick). `roles` lists every role of the config (then orphans that are still paused) as `{"role", "paused", "by", "at", "cause", "rule", "ledger_seq", "in_config"}`.
- **`can_act`** is true exactly when the caller is a signed-in person whose role allows `decide` (or, with `--auth off`, the in-process `dev_human` harness). It only decides whether the app shows the buttons; the server enforces the rule on every POST. A token of any scope, an anonymous caller and a viewer get false.
- **`GET /v1/org/breakers`** answers `{"org", "halt", "evaluation", "breakers"}`. `evaluation.state` is `not_configured`, `starting`, `ok`, `failing` or `stopped`, with `tick_seconds`, `halt_after_failed_ticks`, `last_evaluated_at`, `last_error_*`, `last_failing_rule` and `consecutive_failures` (they describe this server process). Each breaker has `name`, `signal`, `action`, `role`, `window_seconds`, `dry_run`, `limit`, `observed` (decimal text), `observed_as` (`live` or `at_trip`), `unknown_cost_calls`, `window_start`, `state` (`ok`, `tripped`, `disabled`), the `tripped_*` and `disabled_*` fields, `last_reset_at`, `manual` and `in_config`.
- **Order of the checks on every POST:** no credentials (401, or 403 `authentication_required` with `--auth off` and no `dev_human`), then 403 `human_required` (a token, a service, anything that is not a person; before the body is read), 403 `csrf_invalid`, 403 `forbidden` (a viewer), 422 (body), 404, 409. There is no HTTP route to trip a breaker; a manual trip is a command-line drill (`nuramesh breakers trip`).
- **Another organization's caller** gets the missing answer, byte for byte: 404 `no_such_role` or `no_such_breaker` (`application/problem+json`, detail `no such role` or `no such breaker`), the same as an unknown name; the GETs show their own organization (`roles: []`, `breakers: []`, `not_configured`).
- **Fail closed:** when the org stream cannot be read, a run's stop check treats the org as halted (`via: org_halt_unreadable`) and a model call is refused (`model.blocked`, reason `org_halted`). SQLite reads first wait out a transient lock (up to two seconds) so a passing lock is not read as an unreadable ledger.

## Destroyed organization keys (R1.37; spec: the internal specification)

After an organization's content is shredded its key is never recreated. A request that would need the key answers **409** and a problem code, with nothing written to the ledger or the content store:

| Code | Meaning |
| --- | --- |
| `org_key_destroyed` | the organization was erased; the id is retired and cannot be reused. Routes that capture a reason or evidence: approval decision, budget unblock, human gate decision. |
| `org_key_missing` | the organization has stored content but no key and no tombstone (a lost or unadopted key folder); nothing is created. See the runbook. |

An approval that is already decided, not pending, or answered by someone not allowed still answers `approval_refused` first (the check runs before the key is needed). Refusals are counted, not ledgered: one security-log row `content.capture_refused` per organization, day and reason, with a `count`. `POST /v1/config/validate` answers a retired id with 422 `invalid_config`; `org_id_retired` is the issue code inside it, not a second top-level code.

`GET /v1/health` is public, and answers 429 `rate_limited` (with `Retry-After`) to a caller that is not signed in after 120 requests a minute from one client (an IPv6 client counts by its /64); a request with a valid session cookie or API token is never counted, and a bad credential counts as not signed in. `GET /v1/health` has `mail_enabled`, one boolean: true only when outgoing email is fully set up and has a sender (the login page offers "Reset it by email" only then); it never carries a host, sender address or setting name. `GET /v1/health` has a `warnings` list (empty when healthy). `content_capture_failing` is present while the last capture failed for a reason other than a destroyed key (a full disk, an unreadable key folder); it clears on the next successful capture. Failures are counted in one `content.capture_failed` security-log row per organization and UTC day.

## Organization scope and the network gate

- **Every run, ledger, task, approval and event route is scoped to the caller's organization.** The organization comes from the credential (the session's user or the API token), never from the request. A run's organization is the `org` of its first ledger entry (`run.started`); an `org:<id>` stream belongs to `<id>`; any other stream belongs to nobody. **Security entries of the `org:<id>` stream** (`auth.*`, `user.*` and `agent.credential_*`: sign-in times, failed logins, account changes) are shown only to a caller with ADMIN, on `GET /v1/ledger/{stream}` and on `WS /v1/events`; a caller with only READ sees the rest of the stream (halts, budgets, content and the like) and `next_after_seq` still advances past the hidden entries (R1.30 review 1).
- **Another organization's resource is a missing resource.** `GET /v1/runs/{id}`, `/ledger`, `/ledger/verify`, `/stop`, `/report`, `/tasks`, `/tasks/{task}/timeline`, `/tasks/{task}/audit`, `POST /v1/runs/{id}/stop`, `GET /v1/ledger/{stream}` (and `/verify`), `GET /v1/approvals/{id}` and `POST /v1/approvals/{id}/decision` answer 404 `not_found` with the detail `no such run`, `no such stream` or `no such approval` (never the id), byte for byte the answer for an id that does not exist (the status, body and headers, except `Date`, are the same). `WS /v1/runs/{id}/events` closes with 4404 in both cases. A caller without the route's permission gets 403 for any id first. `GET /v1/runs/{id}/tasks` on an unknown run is now 404 (it was `[]`).
- **Lists show only the caller's organization:** `GET /v1/runs`, `GET /v1/ledger`, `GET /v1/ledger/verify` (both now carry `truncated` when one request's lookup bound, 5,000 streams not seen before, cut the list), `GET /v1/approvals` (`?run_id=` of a foreign run is `[]`, like an unknown one), and `WS /v1/events` (a subscription naming a foreign stream is accepted and delivers nothing from it). `GET /v1/runs` looks at no more than 500 runs of the caller's organization per request (another organization's runs are not counted; `truncated` says when a bound cut the list); the budget history reads only `org:<caller's organization>`.
- **Limits, stated honestly:** the ledger streams table has no organization column, so a stream's organization costs one read of its first entry the first time it is asked about (then it is remembered in that caller's organization's own memory, up to 10,000 streams per organization, oldest forgotten first; one organization's lookups never change what another is told). `GET /v1/health` is public and still reports the total stream count of the whole store. Budgets are one table for the store.
- **Route gate.** Every app (loopback or network, with or without sign-in) checks when it is built, and `nuramesh serve` checks again before it listens, that the routes it serves are exactly the routes declared in `nuramesh.server.routes.ROUTE_SCOPES` and `authz.ROUTE_RULES`, and that both agree (permission and class) with the written matrix `nuramesh.server.route_matrix.MATRIX`, which an internal test calls over HTTP with every kind of caller of two organizations. A served route that is not declared, a declared route that is not served, a table that differs from the matrix, or a route the check cannot read (FastAPI's `include_router` wrapper has no path: register routes on the app itself) makes `nuramesh serve` print `refusing to listen: ...` and exit 2, and makes `create_app` raise `RouteTableError`. There is no flag to switch it off. To add a route: add it to `ROUTE_RULES`, `ROUTE_SCOPES`, `route_matrix.MATRIX`, the matrix test's `samples`, the spec's route table, and run the matrix. The check cannot know that the test passed; CI's required check does that before merge.

## Agent credentials

Seven routes, an owner in a signed-in session only (CSRF as for every write; an API token or an agent credential is `403 human_required`). An agent credential itself gets only the narrowed views of its own routes (see the Agent credentials guide). Lists show this server's team only (what the owner can act on); the limit of 5 live credentials counts the agent across the organization's teams. Closing the organization ends every agent credential at once (reason `org_closed`):

| Route | What |
| --- | --- |
| `POST /v1/agents/{id}/credentials` | issue `{scopes: ["read","run"], expires_in_days?: 1-90}`; 201 with the secret in `credential` (shown once, `Cache-Control: no-store`) |
| `GET /v1/agents/{id}/credentials` | the agent's credentials: id, scopes, `active`, dates, `created_by`, `in_config`, `issuer_erased`; never the secret or hash |
| `GET /v1/agents` | owner only: the team's agents for the Agents page `{agents: [{id, name, role, active_credentials}]}`, ordered by id; no fingerprint |
| `GET /v1/agent-credentials?issuer_erased=true` | this team's credentials, optionally only live ones whose issuer was erased |
| `POST /v1/agents/{id}/credentials/{cid}/revoke` | revoke (always allowed, also while halted) |
| `POST /v1/agents/{id}/credentials/{cid}/rotate` | `{expires_in_days?, grace_seconds?}`; 201 with a new secret, the old one ends after the grace (at most 24 hours) |
| `POST /v1/agents/{id}/credentials/revoke-all` | `{"revoked": n}` |

Refusals: `409 credential_limit` (5 live per agent), `409 agent_suspended` (halted or role paused: issue and rotate only), `404 no such agent` / `no such credential` (one answer for unknown, other team and other organization), `422 invalid_request`. An agent presents `Authorization: Bearer nma_...` and may call only: `GET /v1/health`, `GET /v1/agents/{its own id}/eligibility`, `GET /v1/runs`, `GET /v1/runs/{id}`, `GET /v1/runs/{id}/tasks`, `GET` and `POST /v1/runs/{id}/stop` (the stop only for a run it takes part in; any other is the missing-run 404). Everything else is `403 agent_forbidden` or `human_required`. A rejected presentation is always the same `401 token_invalid`; 30 failures in 60 s from one client are `429`, and one credential is capped at 600 valid calls a minute (`429 rate_limited`).

## Costs (`/v1/costs/*`; spec: the internal specification)

Read-only views of what the money bought, computed from the ledger of the session's organization
(no ledger entry, no store write). All times are UTC instants (a time without an offset is 422);
`from`/`to` default to the current UTC month, `as_of` to now and is echoed back. A request that
would read more than 100,000 ledger entries is 422 `range_too_large`. So is a `series=day|month` range of more than 1,000 periods. A quote counts every organization run stream whose newest entry is recent (at or after the end time of the 20th comparable run), a run that is still going and runs with late entries included, so a still-running run with a very long stream blocks quotes with 422 `range_too_large` until it ends. Money is exact decimal text;
a ratio has exactly 6 places; an unknown price is `null`/"at least", never a guess.

| Request | Returns |
| --- | --- |
| `GET /v1/costs/per-result?group_by=none\|role\|provider\|model&from=&to=&as_of=` | `overall` and `rows`: per bucket (`accepted`, `rejected` with `by_reason`, `pending`, `unattributed`) `known_total`, `calls`, `unknown_calls`; accepted also `results`, `retry_known_total`, `provisional_calls`, `complete`, `per_result`, `per_result_at_least`; role rows have `role_source` |
| `GET /v1/costs/spend?group_by=role\|provider\|model&series=none\|day\|month&from=&to=&as_of=` | `periods`, each `start`, `end`, `overall`, `rows` (with `share`) |
| `GET /v1/costs/calls?bucket=&group_by=&group=&from=&to=&as_of=&limit=&after=` | the calls behind a cell, newest first, `limit` 1 to 200 (default 50), `next` cursor |
| `POST /v1/costs/quote` `{"tasks": [{"role", "provider", "model"}], "as_of"?}` | `status` (`ok` or `not_enough_history`), `low`, `median`, `high`, `sample_size`, `minimum_sample`, `excluded`, and `advisory: true`, `blocking: false`, `budget_input: false`. Writes nothing. |
| `GET /v1/savings/tokens?group_by=none\|role\|provider\|model&from=&to=&as_of=` | `rows`, one per group (`all` for `none`): `calls`, `unknown_calls`, `known_total`, `complete`, `input_tokens`, `output_tokens`, `cached_input_tokens`, `cache_write_tokens`, `calls_without_usage`, `cache_hit_rate`, `cache_saving`, `cache_write_premium`, `net_cache_saving`, `unknown_saving_calls`, `saving_complete`, `cache_saving_share`; read only, own organization, same 100,000-entry bound and 422 `range_too_large` as the cost views; rates are the ones recorded on each call, an unknown saving is counted and excluded, never zero |

## Route reference (every route of the app)

One row per route the app serves, from `nuramesh.server.routes`, `authz.ROUTE_RULES` and `IDEMPOTENCY_ROUTES`. An internal test fails if a route of the OpenAPI schema is missing from this file. The details of each group are in the sections above and below.

**Permissions.** `public` needs no credential. `read`, `run`, `decide`, `reveal` and `admin` are checked against the caller: a person's role gives `viewer` = read; `approver` = read, run, decide, reveal; `owner` = all five. An API token (`nmt_...`) gives only its scopes: `read` = read, `run` = read and run; a token never has decide, reveal or admin (403 `human_required` on those). An agent credential (`nma_...`) may call only the routes marked "agent" below (anything else is 403 `agent_forbidden`). A route with no rule is refused (403 `no_rule`, default deny). With `--auth off` `public` and `read` routes are open, `run`, `decide` and `reveal` routes are refused with 403 `authentication_required` except under the in-process test harness `dev_human`, and `admin` routes are refused with the same 403 `authentication_required` even under `dev_human` (the check runs before the organization lookup, so it comes before 409 `no_config`). A write made with a cookie session also needs `X-CSRF-Token` (403 `csrf_invalid`). Idempotency column: `once` = honors `Idempotency-Key` with exactly-once delivery, `at-least` = honors it with at-least-once delivery, `no key` = answers 400 `idempotency_key_unsupported` (secret in the answer) or ignores it (read-only POST), `-` = a read.

| Method and path | Permission | Idempotency | Notes |
| --- | --- | --- | --- |
| `GET /v1/health` | public (agent) | - | `{status: "ok", streams, org, humans_enabled, auth_required, mail_enabled, warnings: [...]}` |
| `GET /v1/openapi.json`, `HEAD /v1/openapi.json` | read | - | the OpenAPI schema |
| `GET /v1/docs`, `HEAD /v1/docs` | read | - | Swagger UI; served only without an organization and without sign-in, else 404 |
| `GET /setup`, `HEAD /setup` | public | - | the first-owner setup page (HTML); 404 with authentication off or once an owner record has existed |
| `GET /v1/schema/config` | read | - | the JSON Schema of the organization configuration |
| `POST /v1/config/validate` | read | no key | body: a configuration object; 200 `{"valid": true}`; 422 `invalid_config` (also `org_id_retired` for an erased organization id); 503 `content_store_unavailable` when the erased-id (tombstone) check cannot be read: it fails closed, never "valid" |
| `POST /v1/auth/login`, `POST /v1/auth/login/mfa`, `POST /v1/auth/login/mfa/enroll`, `POST /v1/auth/login/mfa/enroll/confirm`, `POST /v1/auth/refresh`, `POST /v1/auth/logout`, `GET /v1/auth/session`, `GET /v1/auth/setup/status`, `POST /v1/auth/setup/redeem` | public | no key | see "Accounts and sessions" |
| `POST /v1/auth/invites/inspect`, `POST /v1/auth/invites/accept` | public | no key | see "Accounts and sessions" |
| `POST /v1/auth/me/email` | read, person only | no key | see "Accounts and sessions" (R1.40) |
| `POST /v1/auth/email/verify` | public | no key | see "Accounts and sessions" (R1.40) |
| `POST /v1/auth/password-reset/request`, `POST /v1/auth/password-reset/redeem`, `POST /v1/auth/tokens/revoke-by-link` | public | no key | see "Accounts and sessions" (R1.41) |
| `POST /v1/auth/users/{user_id}/password-reset` | owner | no key | see "Accounts and sessions" (R1.41) |
| `GET /v1/auth/invites`, `POST /v1/auth/invites`, `POST /v1/auth/invites/{invite_id}/revoke`, `POST /v1/auth/invites/{invite_id}/resend` | admin | create and resend: no key; revoke: once | owner in a human session; see "Accounts and sessions" |
| `GET /v1/auth/tokens`, `POST /v1/auth/tokens`, `POST /v1/auth/tokens/{token_id}/revoke`, `PATCH /v1/auth/tokens/{token_id}`, `DELETE /v1/auth/tokens/{token_id}` | admin | create: no key; revoke, rename and delete: once | see "API tokens" |
| `GET /v1/auth/email/status` | admin | - | owner in a human session: `{"enabled", "missing": [variable names], "queue_depth", "verify_queue_depth", "background_failed"}` (names and counts only; email is set up with the `NURAMESH_SMTP_*` variables in `docs/configuration-env.md`) |
| `GET /v1/auth/users` | admin | - | owner in a human session: `{"users": [{id, role, email, display_name, disabled, created_at}, ...]}` |
| `GET /v1/auth/users/{user_id}/agent-credentials` | admin | - | `{"credentials": [...]}`: the live agent credentials the person issued, oldest first; 404 for an unknown person |
| `POST /v1/auth/users/{user_id}/disable` | admin | once | body `{}`: 200 `{user, disabled: true, changed}`; refused for the last owner; also runs unkeyed when the organization key is destroyed; with an `Idempotency-Key` and an unreadable key ring it is 503 `idempotency_unavailable` and does not run (only the three revoke routes run unkeyed then) |
| `POST /v1/auth/users/{user_id}/enable` | admin | once | body `{}`: 200 `{user, disabled: false, changed}` |
| `PUT /v1/auth/users/{user_id}/role` `{role}` | admin | once | 200 `{user, role, changed}`; 422 for a role that cannot be assigned; refused when it would demote the last owner |
| `GET /v1/auth/mfa` | read, person only | - | your MFA state: `{available, enrolled, confirmed_at, recovery_codes_remaining, required}`; an API token or agent credential is 403 `human_required` |
| `POST /v1/auth/mfa/enroll` `{password}` | read, person only | ignored (the header is not read; nothing stored) | 200 `{factor_id, secret, otpauth_uri}` (`Cache-Control: no-store`); 401 `invalid_credentials`; 409 `mfa_already_enrolled`; 503 `mfa_unavailable` without an auth key |
| `POST /v1/auth/mfa/enroll/confirm` `{factor_id, code}` | read, person only | ignored (the header is not read; nothing stored) | 200 `{recovery_codes: [10]}`; 401 `mfa_invalid`; 409 `no_pending_factor`, `reauth_required` (password older than 300 s) or `mfa_already_enrolled`; 429 `too_many_attempts`; other sessions of the person end (`mfa_changed`) |
| `POST /v1/auth/mfa/recovery-codes` `{password, code}` | read, person only | ignored (the header is not read; nothing stored) | 200 `{recovery_codes: [10]}`; the old codes stop working |
| `POST /v1/auth/mfa/disable` `{password, code}` | read, person only | ignored (the header is not read; nothing stored) | 200 `{enrolled: false}`; 409 `mfa_required_by_policy` for an owner while the policy is on |
| `POST /v1/auth/password/confirm` `{password}` | read, a person in a cookie session only (a token or agent credential: 403 `human_required`; authentication off: 403 `authentication_required`) | ignored (the header is not read; nothing stored) | 200 `{expires_in: 600}`: this session's password confirmation is a fresh proof for a solo approval of a high-risk action for 600 s; 401 `invalid_credentials` for a wrong password (counted like a sign-in), 429 `too_many_attempts` while locked, 422 for any other body |
| `POST /v1/auth/users/{user_id}/mfa/reset` | admin | once | body `{}`: 200 `{user, changed}`; all of the person's sessions end (`mfa_changed`); 409 `use_self_service` for yourself |
| `GET /v1/auth/settings` | admin | - | `{mfa_required_for_owners}` |
| `PUT /v1/auth/settings/mfa` `{required}` | admin | once | 200 `{mfa_required_for_owners}`; 409 `mfa_not_enrolled` when turning on without your own factor; 503 `mfa_unavailable` without an auth key |
| `GET /v1/runs` | read (agent) | - | see "Endpoints added or extended" |
| `POST /v1/runs` | run (person only) | at-least | start a run: asynchronous, see "Start a run" |
| `GET /v1/runs/{run_id}` | read (agent) | - | run detail; for a start request a runner has not begun yet (or refused), `{run_id, state: "requested" or "refused", refusal_code, engine, data_class, requested_at}` |
| `GET /v1/runs/{run_id}/ledger`, `GET /v1/runs/{run_id}/ledger/verify` | read | - | the run's stream, paged; chain check |
| `GET /v1/runs/{run_id}/stop` | read (agent) | - | stop state |
| `POST /v1/runs/{run_id}/stop` | run (agent, only a run it takes part in) | once | see "Stop" |
| `GET /v1/runs/{run_id}/report` | read | - | see "Run report, tasks and timeline" |
| `GET /v1/runs/{run_id}/tasks` | read (agent, narrowed) | - | see "Run report, tasks and timeline" |
| `GET /v1/runs/{run_id}/tasks/{task_id}/timeline` | read | - | see "Run report, tasks and timeline" |
| `GET /v1/runs/{run_id}/tasks/{task_id}/audit` | read | - | see "Task audit" |
| `POST /v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decisions`, `POST /v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision` | decide | at-least | human session only; see the table above; the singular path is deprecated (see "Renamed in R"). The body takes an optional `acknowledge_solo` (a boolean; anything else is 422): the organization's only person who can decide sends `true` with a decision that passes the gate, else 403 `acknowledge_required` and nothing is recorded; a failing decision needs nothing more (rule SA-19) |
| `GET /v1/ledger`, `GET /v1/ledger/verify` | read | - | the caller's streams; verify page |
| `GET /v1/ledger/{stream}` | read | - | `?after_seq=` (0 or more, default 0), `?limit=` (1 to 500, default 100); answers `{stream, events, head_seq, head, next_after_seq}` |
| `GET /v1/ledger/{stream}/verify` | read | - | one stream: the stream's verify fields plus `ok`, `verified`, `chain_ok`, `anchor_status`, `note`; counts against the caller's verify budget |
| `WS /v1/events`, `WS /v1/runs/{run_id}/events` | read | - | see "Live view" |
| `GET /v1/approvals` | read | - | `?status=` (an approval status), `?run_id=`; a list of approvals |
| `GET /v1/approvals/{approval_id}` | read | - | see "Approvals" |
| `POST /v1/approvals/{approval_id}/approve`, `POST /v1/approvals/{approval_id}/reject`, `POST /v1/approvals/{approval_id}/decision` | decide | once | see "Approvals"; `/decision` is deprecated |
| `GET /v1/content/{org_id}/{content_hash}` | reveal | - | see "Reveal a stored reason" |
| `GET /v1/budgets`, `GET /v1/budgets/{budget_id}` | read | - | see "Endpoints added or extended" |
| `POST /v1/budgets/{budget_id}/unblock` | decide | once | see "Budget unblock" |
| `GET /v1/reports/downgrade` | read | - | see "Downgrade report" |
| `GET /v1/costs/per-result`, `GET /v1/costs/spend`, `GET /v1/costs/calls`, `GET /v1/savings/tokens` | read | - | see "Costs" |
| `POST /v1/costs/quote` | read | no key | see "Costs"; writes nothing |
| `GET /v1/dashboards/verification`, `GET /v1/dashboards/verifiers`, `GET /v1/dashboards/cost-latency`, `GET /v1/dashboards/approvals` | read | - | see "Dashboards"; read only, the session's organization |
| `GET /v1/agents/{agent_id}/eligibility` | read (agent: its own id only) | - | see "Agent eligibility" |
| `GET /v1/verifiers/{agent_id}/calibration` | read (humans only; an agent credential gets 403 `agent_forbidden`) | - | see "Verifier calibration" |
| `GET /v1/agents`, `GET /v1/agent-credentials`, `GET /v1/agents/{agent_id}/credentials` | admin | - | see "Agent credentials" |
| `POST /v1/agents/{agent_id}/credentials`, `POST /v1/agents/{agent_id}/credentials/{credential_id}/rotate` | admin | no key | the secret is in the answer, shown once |
| `POST /v1/agents/{agent_id}/credentials/{credential_id}/revoke`, `POST /v1/agents/{agent_id}/credentials/revoke-all` | admin | once | see "Agent credentials" |
| `GET /v1/org/status`, `GET /v1/org/halt`, `GET /v1/org/breakers` | read | - | see "Organization halt, role pause and breakers". The status also carries `solo_approval: {solo_now, limit_per_hour, window_days, approved, approved_solo, solo_share, approved_solo_after_removal}`: whether exactly one person can decide now, and the counts of the last 30 days' approved requests, with the solo approvals made within 7 days of an approver being removed (rule SA-18) |
| `POST /v1/org/halt`, `POST /v1/org/roles/{role}/pause` | decide | at-least | person only |
| `POST /v1/org/resume`, `POST /v1/org/roles/{role}/resume`, `POST /v1/org/breakers/{name}/reset`, `POST /v1/org/breakers/{name}/disable`, `POST /v1/org/breakers/{name}/enable` | decide | once | person only |
| `PUT /v1/org/prompt-cache` | admin | once | administrator only; off by default; the CLI is `nuramesh org prompt-cache on\|off` |
| `GET /v1/models/catalog`, `GET /v1/models/assignments`, `GET /v1/models/connections/{provider}/key` | read | - | see "Models" |
| `GET /v1/models/keys/health` | admin | - | see "Models" |
| `GET /v1/models/catalog/events`, `GET /v1/models/resolution`, `GET /v1/models/connections`, `GET /v1/models/connections/events` | admin | - | see "Models" |
| `POST /v1/models/catalog/entries`, `POST /v1/models/catalog/entries/edit`, `POST /v1/models/catalog/entries/approve`, `POST /v1/models/catalog/entries/pause`, `POST /v1/models/catalog/entries/retire`, `POST /v1/models/catalog/entries/remove`, `POST /v1/models/catalog/edit`, `POST /v1/models/catalog/approve`, `POST /v1/models/catalog/pause`, `POST /v1/models/catalog/retire`, `POST /v1/models/catalog/remove`, `POST /v1/models/catalog/prices`, `PUT /v1/models/catalog/regions`, `POST /v1/models/catalog/regions`, `POST /v1/models/assignments`, `PUT /v1/models/assignments/{scope}/{subject}` | admin | once | see "Models" |
| `PUT /v1/models/connections/{provider}`, `POST /v1/models/connections/{provider}/remove` | admin | once | see "Models" |
| `POST /v1/models/connections/{provider}/test` | admin | at-least | see "Models" |
| `PUT /v1/models/connections/{provider}/key` | owner | excluded (secret) | see "Models" |
| `DELETE /v1/models/connections/{provider}/key` | owner | ignored | see "Models" |
| `GET /v1/scorecards` | read | - | per-agent scorecards for the caller's organization; see "Earned autonomy and scorecards" |
| `GET /v1/autonomy/proposals` | decide | - | grant proposals earned from verified results, waiting for a human decision |
| `GET /v1/autonomy/grants` | read | - | the organization's autonomy grants with their state, ceiling and expiry (store time) |
| `POST /v1/autonomy/grants` | admin | once | record a grant from a proposal (a human decision; ledgered) |
| `POST /v1/autonomy/grants/{grant_id}/revoke` | decide | once | revoke a grant at once (ledgered); a check before the revoke committed still sees the grant |
| `PUT /v1/autonomy/ceilings/{role}/{kind}` | decide | once | set a role's ceiling for one kind of action (only stricter than the policy, never looser) |
| `PUT /v1/autonomy/settings/{name}` | admin | once | change one autonomy setting; looser than the default is refused (400 `looser_than_default`), some are `not_settable` |
| `PUT /v1/autonomy/model-change-floor/{role}` | admin | once | set which kinds of grant a model change carries for a role (owner, a person); 403 `human_required` for tokens and agents, 400 `not_floor_kind` |

### API tokens (`/v1/auth/tokens`)

Admin: an owner in a human session (a token or an agent credential is 403 `human_required`). With `--auth off` every one of these routes answers 403 `authentication_required` first, whoever calls and before any organization check. Without an organization (and with authentication on) every route answers 409 `no_config`. All answers carry `Cache-Control: no-store`.

- `GET /v1/auth/tokens` answers `{"tokens": [view, ...]}`. A `view` is `{id, name, actor, scopes, created_by, created_at, expires_at, revoked_at, active}` (`actor` is `service:<id>`; the secret and its hash are never shown).
- `POST /v1/auth/tokens` with `{name, scopes, expires_in_days?}`: `name` 1 to 63 characters (a label for the program, not a person; a reserved name, `cli`, `system`, `api`, `engine`, or one starting `tok_`, is refused), `scopes` a non-empty list of `"read"` and `"run"` (at most 2), `expires_in_days` 1 to 365 (default 90). Unknown fields are refused. 201 with `{token, <view fields>, shown_once: true}`; `token` is the only time the secret is shown. It answers 400 `idempotency_key_unsupported` to a request with an `Idempotency-Key`.
- `POST /v1/auth/tokens/{token_id}/revoke` with `{}` (a body with fields is refused): 200 with the view; an id that is not `tok_` and 24 lowercase hex digits, or an unknown one, is 404 `not_found` ("no such token"). Honors `Idempotency-Key` (exactly once). Without a key the revoke runs as usual.
- `PATCH /v1/auth/tokens/{token_id}` with `{"name": "<new>"}` (exactly that key, a string; anything else is 422 `invalid_request`): renames the token in any state and answers 200 with the view. A malformed or unknown id (or another organization's) is 404 `not_found`, decided before the body is read; the token's current name is 200 and writes nothing; a bad name is 422 (the pattern text, or the fixed text `that name is reserved` for `cli`, `system`, `api`, `engine` and anything starting `erased-`, or the `tok_` text; the text never echoes the value); a name held by another token, or **reserved** (below), is 409 `token_name_taken` with one answer for both; an organization with no usable identifier key is 503 `store_busy` and nothing is written. Ledger: one `auth.token_renamed` (payload `{token_hash}`, never a name). Honors `Idempotency-Key` (exactly once).
- `DELETE /v1/auth/tokens/{token_id}` removes a token that is **not active** (revoked, or past `expires_at`): 200 with the final view (`Cache-Control: no-store`). An active token is 409 `token_active` (revoke it first) and nothing changes; an unknown id is 404 `not_found`; no usable key is 503 `store_busy`. The token's revoke links go with it, the ledger entries naming `service:<id>` stay and verify, and presenting its secret afterwards is 401 `token_invalid`. Ledger: one `auth.token_deleted` (payload `{token_hash}`). Honors `Idempotency-Key` (exactly once). Like every mutating route it needs `Content-Type: application/json` and a `Content-Length` (the body may be empty).
- **Reserved names.** A renamed or deleted name stays reserved so a name keeps addressing one token for a while: create or rename to it is 409 `token_name_taken`, the same answer as a live clash. It is stored only as a keyed hash under the organization key (`api_token_retired_names`), and the reservation ends between 1 hour and 90 days + 1 hour after the retirement (the next epoch of the organization key plus its lockout hour). When a token's issuer is erased, the token's name becomes `erased-<id>` and `created_by` (and a `revoked_by` naming that person) becomes `erased-user`; one count-only `auth.token_names_erased` entry records it. The replaced name is not reserved.

### Run report, tasks and timeline

All three need `read` and the run to belong to the caller's organization (another organization's run is 404 `not_found`, "no such run").

- `GET /v1/runs/{run_id}/report` needs an organization (409 `no_config` without). A run with no tasks is 404. Answers the run report with person text redacted: `{run_id, counts, tasks: [...], budgets: [...], pending_approvals: [...], reconciled, ledger: {seq, head, ok}, canon_commit, stopped, stop_requested_by}`.
- `GET /v1/runs/{run_id}/tasks` answers a list: for a person or token, each stored task as JSON plus `version`; for an agent credential, only that agent's own narrowed view of the tasks.
- `GET /v1/runs/{run_id}/tasks/{task_id}/timeline` answers a list of the task's ledger entries (the same objects as the ledger page, with a person's `reason` and `note` text redacted to `null` and the hidden keys named in `redacted`). A task that does not exist or belongs to another run is 404 `not_found` ("no such task").

### Approvals

- `GET /v1/approvals/{approval_id}` answers the approval (`id, run_id, task_id, agent_id, action, asked_because, status, requested_at, decided_by, decided_at, decision_reason_hash, decision_reason, consumed_at`) plus `version`, `ledger` (the `approval.requested` and `approval.decided` entries naming it: `{ledger_seq, type, at, actor}`) and `reason_available`. `decision_reason` is `null` unless the decider may show text. Missing or another organization's approval: 404 `not_found` ("no such approval").
- `POST /v1/approvals/{approval_id}/approve` and `POST /v1/approvals/{approval_id}/reject` with `{reason: string 1 to 1000 characters}` (unknown fields refused, `approve` included) do what `/decision` does with `approve: true` or `false` and write the same `approval.decided` entry; a rejection is stored as `denied`.
- Deprecated: `POST /v1/approvals/{approval_id}/decision` with `{approve: bool, reason: string 1 to 1000 characters}` (unknown fields refused). Human only (`decide`; a token is 403 `human_required`). Needs an organization and the content store (`reason` is kept there). 200 with the updated approval. Refusals: 409 `approval_refused` (already decided, not pending, or the person is not allowed to decide it by the policy; this check runs before the key is needed), 409 `org_key_destroyed` or `org_key_missing`, 503 `content_store_unavailable` when the content store is not configured or cannot be used. Honors `Idempotency-Key` (exactly once), so a replay returns the first answer.

### Budget unblock

`POST /v1/budgets/{budget_id}/unblock` with `{reason: string 1 to 1000 characters, new_limit?: string}`. Human only (`decide`). `new_limit` is a plain decimal text such as `20` or `20.50` (at most 40 characters), in the budget's own currency; anything else is 422 `invalid_amount`. 200 with `{id, blocked, limit}` (`limit` is money). Refusals: 404 `not_found` ("no such budget", also another organization's), 409 `ambiguous_budget` (the local inspector only, several organizations hold that id), 409 `unblock_refused` (a budget that predates an internal decision has no organization, or the unblock was refused by the rules; the first line of the reason, at most 300 characters), 409 `org_key_destroyed` or `org_key_missing`, 503 `content_store_unavailable` (no content store). Honors `Idempotency-Key` (exactly once).

### Reveal a stored reason

`GET /v1/content/{org_id}/{content_hash}`: the text behind a keyed hash in the ledger. `reveal` permission (approver or owner in a human session; never a token, never a viewer). Only this server's own organization and only a person's reason. `content_hash` must be 64 hex digits and `org_id` a slug, else 422 `invalid_hash`. Answers `{hash, text}`; a hash never stored, expired, erased, not a reason, or of another organization is 404 `not_found`. Tampered bytes are 500 `content_unreadable`; a key or storage fault is 503 `content_store_unavailable`. Reading writes one ledger entry, `content.read` (who and which hash, never the text). Gate evidence (kind `gate_evidence`: the stored results of a round that waits for a person, R1.32) is never returned by any route; this one answers 404 `not_found` for it, like any other kind that is not a reason.

### Agent eligibility

`GET /v1/agents/{agent_id}/eligibility` needs an organization. An agent credential may ask only for its own id (else 403 `agent_mismatch`, before anything is read). An id that is not an agent of the organization is 404 `not_found`. With no evals configured and no preset suites for the agent's role: `{agent, eligible: true, reasons: [], evals: false}`. Otherwise `{agent, eligible, reasons, evals: true, recent_results}` (the last five eval records of that agent, without `outcomes`).

### Verifier calibration

`GET /v1/verifiers/{agent_id}/calibration` (read; R2.08) answers whether a verifier is calibrated for each calibration suite of the organization: `{agent, calibrated, suites: [{suite, calibrated, reason, latest, trend, recent_results}]}`. `latest` and `recent_results` (the last five, newest first) hold ids, counts and rates only, never a sample output or a reply; `trend` is the change in catch rate against the previous counting result, or `null`. An agent credential is refused (403 `agent_forbidden`, the agent routes stay seven); an unknown id is 404 `not_found`; an unreadable stored result is 503 `calibration_unavailable`. The route only reads: there is no POST, PUT or DELETE (a calibration is started with `nuramesh calibrate run`).

## Dashboards (`/v1/dashboards/*`, R2.11)

Four read-only views computed from the ledger of the **session's organization** on read (nothing is
stored or written; the figures follow the R2.11 specification). `GET` only. The routes
take no organization: a query parameter named `organization` or `org`, or any parameter other than
`from`, `to`, `as_of`, is 400 `bad_request`, and a credential with no single organization is 403
`no_organization`. They need the same read permission as `/v1/costs/*`. `from`/`to`/`as_of` are
ISO-8601 instants with an offset; the default window is the 7 days ending at `as_of` (default now);
a bad or reversed window is 400 `bad_window` (`from` must be before `to`; an empty window is refused), one over 400 days 400 `window_too_long`. A run of your organization whose ledger holds a time that is not real makes every dashboard 409 `damaged_run` for your organization only. Ratios are
decimal text with 4 places (half-even), `null` with no denominator; money is exact decimal text.
Entries read are capped by `NURAMESH_DASHBOARD_MAX_EVENTS` (default 1,000,000): past it the answer
is `partial: true` with `scanned_events`. At most `NURAMESH_DASHBOARD_MAX_CONCURRENCY` (default 2)
computations run at once (429 `busy`, `Retry-After: 5`); an answer is cached 30 s per organization,
route, window and `as_of` slot, in memory.

| Request | Returns (besides `as_of`, `from`, `to`, `organization`, `partial`, `scanned_events`, `dropped_values`) |
| --- | --- |
| `GET /v1/dashboards/verification` | `passed`, `failed`, `error`, `pass_rate`, `gates`, `roles` |
| `GET /v1/dashboards/verifiers` | `verifier_passed`, `verifier_failed`, `catch_rate` (the app calls it the verifier fail rate), `unknown_kind_results` (gate results with no recorded `gate.started`, for example a ledger written before the OpenTelemetry export recorded gate starts; they are never counted as verifier results), `rounds_to_pass`, `done_without_verifier_pass` |
| `GET /v1/dashboards/cost-latency` | `currencies`, `models`, `roles`, `p50`, `p95`, `max`, `latency_skipped` |
| `GET /v1/dashboards/approvals` | `waiting`, `oldest_waiting_seconds`, `waiting_by_action`, `decided_approved`, `decided_denied` (a state figure at `as_of`) |

## Mapping to the app's `ApiClient` (for app developers)

| Client call | Server |
| --- | --- |
| `runs.list`, `runs.get` | `GET /v1/runs`, `GET /v1/runs/{id}` (title, template, graph and the latest-gate card are derived client-side from tasks, gates and verdicts; the API has no run title or template) |
| `runs.events(id, cursor)` | `GET /v1/runs/{id}/ledger?after_seq=` (category filter: `type` prefix) |
| `runs.subscribe` | `WS /v1/runs/{id}/events?after_seq=` |
| `runs.stop` | `POST /v1/runs/{id}/stop` |
| `runs.pause`, `runs.start` | not available (R1.20) |
| `ledger.list`, `ledger.verifyChain` | `GET /v1/runs/{id}/ledger`, `GET /v1/ledger/verify` |
| `approvals.list`, `get` | `GET /v1/approvals`, `GET /v1/approvals/{id}` |
| `budgets.list` | `GET /v1/budgets` (period and windows: read `definition`) |

**Waking a durable run.** Waking a decision works today; resuming a run after a crash works on the light engine; the Temporal production profile is not shipped yet. `nuramesh serve` wakes the Temporal workflow after a human decision when `NURAMESH_TEMPORAL_ADDRESS` (`host:port`) is set (needs the `temporal` extra of the wheel, else exit 2); `NURAMESH_TEMPORAL_NAMESPACE` picks the namespace (default `default`). Connecting and signalling share a 3 s limit; a failure is `woken: false` and never undoes the decision. Without the address `woken` is `null`. The Temporal dev server is for evaluation only; production needs a real Temporal cluster.

**Expiry warnings.** A task that waits in `verifying` for a human gate is warned 14 and 3 days before its input expires at 90 days. `GET /v1/runs/{run_id}` shows `tasks[].wait_warning` (`days_left`, `expires_at`; `null` otherwise) to people who may decide. When email is configured the server also sends one fixed, content-free mail per approver; see the durable runs page of the docs site. The warning works today; resuming the run after a crash works on the light engine; the Temporal production profile is not shipped yet.

**No wake while halted.** A decision on a halted organization is recorded but no signal is sent: the answer says `woken: false`, `held: true`, `held_reason: "org_halted"` (`halt_unreadable` when the halt state cannot be read). When the halt is lifted (`POST /v1/org/resume`) each held decision is delivered exactly once; a failed delivery is retried after 60 s and 120 s, and parked after three failures (`held_reason: "delivery_pending"`, then `"delivery_parked"`; `GET /v1/health` then lists the warning `decision_wake_parked` for 7 days). A replay of a decision never sends a wake of its own. An organization whose id was erased, or whose key is lost, keeps the existing 409 (`org_key_destroyed`, `org_key_missing`). `POST /v1/org/resume` answers 409 `org_erased` for an erased organization (nothing is appended). Decisions committed without a marker (a crash) are found by a reconcile sweep at server start and every `NURAMESH_DECISION_SWEEP_SECONDS` (a positive whole number, default 300) once they are 30 s old, at most 100 per pass, however old; the CLI has no resume command.

## Downgrade report (`GET /v1/reports/downgrade`)

Read-only: it reads the store and the ledger, writes nothing and **never switches a model**; a report is never recorded in the ledger and never applied (automatic switching is enterprise). It covers the runs of the server's organization only (a run is the organization's when its `run.started` entry names it) and is bounded like the runs list (500 runs, 50,000 ledger entries; `truncated: true` says a bound stopped it). Query: `min_sample` (1 to 10,000, default 30 tasks), `from`, `to` (a UTC instant with an offset; default the current UTC month) and `as_of` (default now); otherwise 422 `invalid_request`. `min_results` is retired and ignored.

**Changed (R1.34, breaking):** the report has one sample threshold and one answer. `min_results` (default 20) and each model's `suggestion` are removed; use `min_sample` (default 30 tasks) and the per-role `recommendation`. The pass rate is now counted per attempt over tasks in a window (`from`, `to`, `as_of`), the model-family check of the verifier and the organization's eligibility rules apply, and the response adds `window`, `excluded` and per-role `evidence`. A caller that sent `min_results` is unaffected (the parameter is ignored); a caller that read `suggestion` must read `recommendation`.

```json
{
  "org": "demo-azure", "read_only": true, "min_sample": 30, "runs_scanned": 4, "truncated": false,
  "window": {"from": "2026-11-01T00:00:00Z", "to": "2026-12-01T00:00:00Z", "as_of": "2026-11-02T12:00:00Z"},
  "excluded": {"mixed_model": 0, "not_evidence": 3},
  "roles": [{"role": "dev",
    "models": [{
      "model": "azure-openai/gpt-5.4", "tasks": 20, "calls": 20,
      "gate": {"passed": 18, "failed": 2, "error": 0},
      "verifier": {"passed": 0, "failed": 0, "error": 0},
      "results": 20, "pass_rate": "0.9000",
      "cost_per_task": {"amount": "0.10", "currency": "USD"}, "cost_known": true, "unknown_cost_calls": 0
    }],
    "evidence": {"azure-openai/gpt-5.4": {
      "tasks": 36, "attempts": 38, "passed": 34, "first_try": 32, "pass_rate": "0.894737",
      "first_try_rate": "0.888889", "known_total": "6.624", "unknown_calls": 0, "cost_per_accepted": "0.194824"}},
    "recommendation": {"status": "recommend", "current": "azure-openai/gpt-5.4",
      "candidate": "azure-openai/gpt-5.4-mini", "refused": [], "saving": "0.814906",
      "projected_saving": "5.397939", "pass_rate_current": "0.894737", "pass_rate_candidate": "0.942857",
      "tasks_current": 36, "tasks_candidate": 34}
  }]
}
```

- **Per-model gate counts (`models`)** keep their meaning: a task belongs to the role and the model of its assignee's own `model.called` entries (a verifier's model is not the worker's cost). A *gated result* is a `gate.completed` entry of that task, attributed to the model of the assignee's latest call before it; `verifier` gates go in `verifier`, `test_command`, `rule`, `schema` and `custom` gates in `gate`, `human` gates are left out. `passed` and `failed` are verdicts; `error` is shown but is neither. `pass_rate` is passed over passed plus failed across both (four decimals, `null` with none). `cost_per_task` is the known spend divided by the tasks (8 places for display); with any unpriced call it is `null` and `cost_known` is false: unknown, never guessed. These rows cover the whole bounded scan of the ledger and ignore `from`, `to` and `as_of`, while `evidence` and `recommendation` follow the window and `as_of`; so one role can show two bases (all scanned tasks in `models`, the windowed tasks in `evidence`).
- **Evidence (`evidence`, per role and model key `provider/model`):** only finished tasks count. A task whose first call by the role is in `[from, to)` and by `as_of`, whose worker calls (a verifier gate's calls carry `gate` and never count) all use one model. **Pass** = the task is `done`; **fail** = it is parked in `needs_human` by a move that closed an attempt, or was cancelled after such a move without reaching `done`; everything else (open, retrying, a gate that only errored, cancelled with no failed attempt) is left out and counted in `excluded.not_evidence`; a task that used two models is `excluded.mixed_model`. `excluded` counts (task, role) units, so a task with worker calls by two roles is counted once per role. `attempts` = closed attempts plus the pass, so a retry is a failed attempt and a pass; `pass_rate` is `passed / attempts` and `first_try_rate` is per task (six places, half away from zero). `known_total` is the exact sum of the known costs of those tasks including failed attempts; `cost_per_accepted` is that over `passed`, and `null` when any call is unknown or nothing passed.
- **The rule for `recommendation` (one per role with evidence):** the *current* model is the one with the most tasks (a tie goes to the smaller key). With fewer than `min_sample` tasks on the current model or on every other model: `insufficient_evidence`. A candidate has at least `min_sample` tasks, a known and **strictly lower** `cost_per_accepted`, and a per-attempt pass rate **at least** the current's, compared exactly on the counts (no tolerance). It is refused (`refused: [{model, reason}]`, ordered by key) as `not_eligible` when the organization's catalog does not allow it for the role, or `same_family_as_verifier` when its model family is that of a verifier model checking the role. Otherwise the largest `saving` wins (a tie goes to the smaller key): `status: recommend` with `saving`, `projected_saving`, both pass rates and both task counts; with none, `status: none`. Eligibility is read at request time: an `approved` catalog entry whose processing scope accepts at least the data classes the current model accepts (with no catalog every model is eligible: this is an approximation of the model resolver, which refuses every non-local model when there is no catalog, `core/model_governance.py`, and eligibility does not look at the run's data class or the adapter's support, T188e (3)). The report states the evidence next to the claim and never says "safe". The rule and its cases are in the repository tests.

**Order of the checks.** On every mutating route that is not public
(38), who is asking is decided right after the Host, TLS and Origin checks and before the content
type, the length and the body: no credentials 401 `authentication_required`, a bad bearer 401
`token_invalid`, a session without the CSRF header 403 `csrf_invalid`, an API token or agent
credential on a decide or admin route 403 `human_required`. Then 415, 411, 413, 422, then the
lookup (404) and the domain refusals (409). The approval, unblock and stop bodies are strict: a
string or number where `approve` is a boolean is 422.

## Earned autonomy and scorecards

Every route is the server's organization only. A refusal appends nothing. A token is never a person: decisions are `human_required` for it.

| Route | Permission | Notes |
| --- | --- | --- |
| `GET /v1/scorecards?kind=agent\|role&id=&from=&to=&as_of=&min_results=` | read | one scorecard, or `{items}` for every subject of the kind; `approvals` also has `approved_solo`, `denied_solo` and `solo_share` (the part decided by a sole approver; `null` when nothing was approved). 400 bad period or `min_results`, 404 unknown subject, 422 `range_too_large`, 503 `scorecard_unavailable` |
| `GET /v1/autonomy/proposals?role=&agent=&as_of=` | decide | `{items, ledger_position, as_of}`; a viewer is 403; echo `ledger_position` and `evidence_hash` in the grant. Each item has `solo: {approved_asks_solo, solo_share}` beside `evidence` (outside `evidence_hash`): how many of the approved asks nobody else reviewed |
| `GET /v1/autonomy/grants?status=active\|all&as_of=` | read | grants with their state and why |
| `POST /v1/autonomy/grants` | admin (owner, a person) | `subject{kind,id}`, `rule_key`, `as_of`, `ledger_position`, `evidence_hash`, `expires_in_days`, `reason`; 201. 409 `evidence_changed`, `not_proposable`, `already_granted` |
| `POST /v1/autonomy/grants/{grant_id}/revoke` | decide | `reason`; 409 `not_active` |
| `PUT /v1/autonomy/ceilings/{role}/{kind}` | decide | `value` `none\|allow`, `reason`; raising is the owner's; 400 `never_grantable` for `spend`, `external_message`, `merge`, `network` |
| `PUT /v1/autonomy/settings/{name}` | admin | thresholds can only be made stricter; 400 `looser_than_default`, `not_settable` |
| `PUT /v1/autonomy/model-change-floor/{role}` | admin | `kinds` (a list of `delegate`, `call_model`, `use_tool`, `write_file`), `reason`; the same value writes nothing; 400 `not_floor_kind` |

`503 autonomy_unavailable` means the grants or evidence could not be read; a policy check then stays `ask`. The CLI has the same reads and decisions: `nuramesh scorecards show`, `nuramesh autonomy propose|grants list|grants create|grants revoke|ceilings set|settings set` (decisions need `--human human:<id>`; the CLI acts with the owner's rights because it opens the team's own store).

## Renamed in R

Each old path below is served by the same handler for one release and answers with `Deprecation: @1796083200` (RFC 9745, 2026-12-01), `Sunset: Mon, 01 Mar 2027 00:00:00 GMT` (RFC 8594, at least 90 days later) and `Link: <successor>; rel="successor-version"`. The new path carries none of the three, and OpenAPI marks the old operation `deprecated`. The old paths go no earlier than the Sunset date. An idempotency key belongs to the path it was first used on.

| Old | New |
| --- | --- |
| `GET /v1/org/halt` | `GET /v1/org/status` (`POST /v1/org/halt` stays) |
| `POST /v1/approvals/{id}/decision` `{approve, reason}` | `POST /v1/approvals/{id}/approve` or `/reject` `{reason}` |
| `POST /v1/runs/{id}/tasks/{task}/gates/{gate}/decision` | `.../gates/{gate}/decisions` |
| `POST /v1/models/catalog/edit`, `/approve`, `/pause`, `/retire`, `/remove` | `POST /v1/models/catalog/entries/edit`, `/approve`, `/pause`, `/retire`, `/remove` |
| `POST /v1/models/catalog/regions` | `PUT /v1/models/catalog/regions` |
