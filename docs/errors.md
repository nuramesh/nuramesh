# Error codes

Every error answer of the API is `application/problem+json` with a stable `code` (the `title` and
the tail of the `type` urn), the HTTP `status`, and a `detail` written for people. Match on the
`code`, never on the `detail`. This page lists every code the server (and anything it calls in
the engine and core) can answer with, what it means and how to fix it.

This page is kept current by `tests/test_error_catalogue.py`: it scans the source with
`scripts/error_sites.py` and fails when the code raises a code that is not listed here, or this
page lists a code nobody raises. Trigger: the test runs in CI.

To add a code: raise it with literal status and code arguments, then add one row below (the
codes are sorted). A status of `any` means the status of the underlying HTTP error.

| Code | HTTP | What it means | How to fix it |
| --- | --- | --- | --- |
| `acknowledge_required` | 403 | You are the only person who can approve this, and the approve did not carry `acknowledge_solo: true` or the `action_digest` of the approval you were shown; or you are the only person who can decide a human gate and a decision that passes it did not carry `acknowledge_solo: true`. Nothing was recorded. | Read the approval again and confirm the solo approval (send both fields); for a gate, send `acknowledge_solo: true` with the decision. A reject and a failing gate decision need neither. |
| `action_changed` | 409 | The `action_digest` sent with a solo approve is not the digest of this approval: the action is not the one you were shown. Nothing was recorded. | Read the approval again and approve what it shows now. |
| `agent_forbidden` | 403 | The route is not open to agent credentials. | Call it as a signed-in person, or use a route that agents may call. |
| `agent_mismatch` | 403 | An agent credential asked about the eligibility of a different agent than its own. | Ask about the credential's own agent, or use a person's session. |
| `agent_suspended` | 403, 409 | The organization is halted or the agent's role is paused, so the agent cannot act and credentials cannot be issued or renewed (they can still be revoked). | Lift the halt or resume the role, then retry. |
| `already_decided` | 409 | The human gate already has a decision in this round. | Read the task to see the recorded decision; nothing to do. |
| `already_disabled` | 409 | The breaker is already disabled. | None; the state you asked for already holds. |
| `already_exists` | 409 | The record (an account, or a model in the catalog) already exists. | Use the existing one, or pick another name or reference. |
| `already_granted` | 409 | An active autonomy grant already exists for this role and action. | Use the grant, or revoke it before proposing again. |
| `already_halted` | 409 | The organization is already halted. | None; the halt is in force. |
| `already_paused` | 409 | The role is already paused. | None; the pause is in force. |
| `already_running` | 409 | You asked to resume an organization that is not halted. | None; the organization is running. |
| `ambiguous_budget` | 409 | Several organizations have a budget with this id. | Sign in as the one organization that owns the budget, then retry. |
| `approval_refused` | 409 | The approval cannot take that answer. The detail says why: it is no longer pending, the answerer is not a human or may not answer it, or no reason was given. | Fetch the approval and check its state; answer a pending one as an allowed human, with a reason. |
| `authentication_required` | 401, 403 | The route needs a signed-in person and the request has none. | Sign in (the server was started with `--auth off`: restart it without that flag), then retry. |
| `autonomy_unavailable` | 503 | Earned autonomy cannot be computed (the grants or the evidence cannot be read), so the request fails closed. | Retry later; if it persists, check the store and the ledger for damage. |
| `bad_ledger_entry` | 500 | A `model.called` ledger entry could not be read as a cost. The cost view refuses to guess. | Report it to the operator; the ledger entry needs inspection. |
| `bad_request` | 400 | A dashboard request carries a query parameter that is not allowed (an `organization` or `org` parameter: the organization is always the session's, never an input). | Remove the parameter; sign in as a member of the organization you want. |
| `bad_window` | 400 | A dashboard time window is not valid: a timestamp that does not parse or is out of range, or `from` not before `to` (an empty window is refused too). | Send ISO 8601 UTC instants with `from` not after `to`. |
| `breaker_tripped` | 409 | The breaker is tripped, so it cannot be disabled. | Reset the breaker first, then disable it. |
| `busy` | 429,503 | The server is busy (hashing passwords, computing dashboards, verifying ledgers (at most 8 at once), or sending mail: 503 when the mail slots are taken); the request was not tried. | Retry after the `Retry-After` seconds. |
| `calibration_unavailable` | 503 | A stored calibration result of the organization is unreadable, so the route refuses to show a partial answer. | Retry later; if it persists, the ledger needs inspection. |
| `challenge_invalid` | 401 | The sign-in step is unknown, used, expired or does not match; one answer for every cause. | Sign in again with your password. |
| `conflict` | 409 | The data changed while the request ran. | Read the current state and retry. |
| `connection_changed` | 409 | The connection was edited while a new key was being tested, so nothing was activated (the edit stays). | Read the connection again and set the key again. |
| `content_store_unavailable` | 503 | The reason text cannot be kept: the content store is not configured, or it failed. Nothing was changed. | Install the `content` extra and set the key directory for the server, or fix the key directory, then retry. |
| `content_unavailable` | 503 | A model-catalog or connection write needs a reason, and the content store is not available. Nothing was written. | Configure the content store (see `content_store_unavailable`), then retry. |
| `content_unreadable` | 500 | The stored content failed its integrity check, so it is not shown. | Report it to the operator; restore the content from a backup. |
| `creator_inactive` | 409 | The owner who made this invitation is disabled, erased or no longer an owner. | Create a new invitation. |
| `credential_limit` | 409 | The agent already has the maximum number of live credentials. | Revoke an old credential, then issue the new one. |
| `credential_not_active` | 409 | The credential is not active, or the agent's definition changed after it was issued. | Issue a new credential for the agent. |
| `csrf_invalid` | 403 | The `X-CSRF-Token` header is missing or wrong on a state-changing request made with a session cookie. | Send the CSRF token from sign-in in the `X-CSRF-Token` header. |
| `cursor_invalid` | 400 | The `cursor` of a list request is unreadable, belongs to another route, or names a row that no longer exists. | Start again without a cursor and follow `next_cursor` from the first page. |
| `customer_data_not_enabled` | 422 | The organization has not enabled customer data, so a run start that keeps plan text is refused. | Customer data is a deployment switch (`NURAMESH_CUSTOMER_DATA_ENABLED`) the deployment owner turns on after the legal documents are in place; it is not an organization setting. Retry once the deployment has it. |
| `damaged_run` | 409 | A run in the organization holds a stored time that is not real, so the cost views and the dashboards cannot be answered. Only that organization is affected. | The operator must repair the run's stream; then retry. |
| `decider_not_allowed` | 403 | This person is not allowed to decide this human gate (default deny). | Ask a person the gate allows to decide. |
| `email_not_configured` | 503 | Email is not set up on this server (no mail host, sender or public URL). | The operator sets `NURAMESH_SMTP_*` and `NURAMESH_PUBLIC_URL`; an operator can use `--print-link` on the CLI. |
| `email_send_failed` | 502 | The invitation email could not be sent; the invitation was revoked. | Check the mail settings, then invite again. |
| `email_taken` | 409 | A user with this email address already exists. | Sign in with that account, or use another email. |
| `email_unsupported` | 409 | The person's address cannot be mailed (it is not plain ASCII), so no password-reset mail was sent. | Change the address to one that can be mailed, then retry. |
| `email_unverified` | 409 | The person's address was never confirmed, so no password-reset mail was sent. | The person confirms the address first, then retry. |
| `endpoint_id_not_allowed` | 422 | A named endpoint id was sent for a provider that has one fixed endpoint; only the OpenAI-compatible provider takes an endpoint id. | Drop `endpoint_id`, or use the `openai-compatible` provider. |
| `engine_unavailable` | 503 | No engine can take a run start right now. | Retry later; the operator checks the engine and the runner. |
| `entitlement_required` | 403 | The feature is not granted by the licence. This is not a payment request. | Add the feature to the organization's entitlement, or do not use the feature. |
| `evidence_changed` | 409 | The evidence you looked at is no longer what the ledger says, or a trigger was recorded after you looked. | Fetch the proposal again and decide on the new evidence. |
| `forbidden` | 403 | The caller's role, token scopes or credential scopes do not allow the action, or the acting person is no longer an owner. | Use an account or token with the needed role or scope. |
| `foreign_origin` | 403 | A cross-site request was refused. | Call the API from the same origin as the app, or from a non-browser client. |
| `hosted_refused` | 403 | Prompt caching cannot be switched on or off in hosted mode (or `NURAMESH_HOSTED` is unreadable) until the privacy notes are published. | None for now; nothing was recorded. Change it on a self-run deployment. |
| `http_error` | any | An HTTP error with no more specific code (the status is the one returned). | Read the status and `detail`; fix the request. |
| `human_decision_refused` | 409 | The human decision was refused for a reason with no more specific code. | Read the `detail` and the task state, then retry if it still applies. |
| `human_required` | 403 | Only a signed-in person may do this; an agent credential or token cannot. | Do it as a signed-in person. |
| `idempotency_in_progress` | 409 | Another request with the same `Idempotency-Key` is still running. | Wait a moment and retry with the same key. |
| `idempotency_key_invalid` | 400 | The `Idempotency-Key` is malformed: it must be sent exactly once, 16 to 128 characters of letters, digits, `.`, `_` or `-`. | Send one well-formed key per request. |
| `idempotency_key_reused` | 422 | The key was already used with a different request. | Use a new key for a different request; reuse a key only to retry the same request. |
| `idempotency_key_unsupported` | 400 | This route does not take an `Idempotency-Key`. | Remove the header. |
| `idempotency_response_unavailable` | 409 | The original request was carried out, but its stored response can no longer be shown. | Check the current state, then use a new key if you must repeat it. |
| `idempotency_unavailable` | 503 | The key ring or the store behind idempotency cannot be used right now. Retry-After is 5 seconds. | Retry after the `Retry-After` seconds; if it persists, check the key ring and the store. |
| `in_use` | 409 | The catalog entry is still assigned to roles or agents, so it cannot be removed. | Reassign the roles or agents that use it (the detail names them), then remove it. |
| `internal_error` | 500 | An unexpected server error. Nothing about the cause is shown. | Retry; if it persists, report it with the time and the request. |
| `invalid` | 400 | The autonomy setting value is not valid (an unknown setting name, a number below its floor, the wrong shape, or the streak settings contradict each other). The detail says which. | Send a value the detail allows. |
| `invalid_amount` | 422 | The budget limit is not a plain decimal. | Send a plain decimal such as `20` or `20.50`. |
| `invalid_config` | 422 | The configuration has problems; the `issues` list names each path. | Fix the paths in `issues` and validate again. |
| `invalid_credentials` | 401 | The email or password is not right. | Check both and retry; repeated failures lock the account or your address for a while (`too_many_attempts`). |
| `invalid_decision` | 422 | The gate refused the decision as invalid; the detail says why. | Fix the decision body as the detail says. |
| `invalid_hash` | 422 | The content reference is not an organization id and a 64-digit hash. | Send the id and hash exactly as the ledger shows them. |
| `invalid_host` | 400 | The `Host` header is not one the server answers for. | Call the server by an allowed host name. |
| `invalid_invite` | 403 | This invitation link is not valid (wrong, used, expired or revoked: one answer for all). | Ask the owner for a new invitation. |
| `invalid_period` | 400 | The period is reversed (`from` after `to`) or longer than the maximum days. | Send `from` before `to` and a shorter period. |
| `invalid_request` | 400, 422 | The request body or query is not valid; the detail (and `problems`, if present) names the field. | Fix the named field and send again. |
| `invalid_reset_token` | 403 | The password-reset link is wrong, expired, used or revoked (one answer for every case). | Ask for a new reset link. |
| `invalid_revoke_link` | 403 | The token revoke link is wrong, expired or used (one answer for every case). | Revoke the token from the app instead. |
| `invalid_setup_token` | 403 | The setup token is wrong, used or expired. The answer is the same for all three on purpose. | Ask the operator for a new setup token. |
| `invalid_verification_token` | 403 | This verification link is not valid (wrong, used, expired, cancelled: one answer for all). | Start the email change again. |
| `invite_limit` | 409 | Too many pending invitations (`NURAMESH_INVITE_MAX_PENDING`). | Revoke some, or wait for them to expire. |
| `invite_not_pending` | 409 | The invitation was accepted, revoked or has expired. | Create a new invitation. |
| `invite_pending` | 409 | That address already has a pending invitation. | Resend or revoke it. |
| `key_ref_server_only` | 422 | A connection body carried `key_ref`, which only the key route writes. | Remove `key_ref`; use `PUT /v1/models/connections/{provider}/key`. |
| `key_required` | 422 | A model connection was asked for with a provider the server does not support; the detail lists the supported providers. | Use one of the supported providers listed in the detail. |
| `key_test_failed` | 422 | The connection test of a new key version failed; the key is not active and the connection is unchanged. The body names the fixed `failure_code`. | Check the key at the provider, then set it again. |
| `last_owner` | 409 | This is the last active owner of the organization. | Transfer ownership to another owner first. |
| `length_required` | 411 | The request had no plain `Content-Length` (chunked bodies are refused). | Send a `Content-Length`. |
| `looser_than_default` | 400 | The autonomy setting is looser than the safe default, which a person may not loosen. | Choose a value at least as strict as the default. |
| `method_not_allowed` | 405 | The route does not take this HTTP method. | Use the method in the API reference. |
| `mfa_already_enrolled` | 409 | Multi-factor sign-in is already on for this person. | Nothing to do, or turn it off first to enrol again. |
| `mfa_enrollment_required` | 403 | A hosted deployment needs the owner to enrol multi-factor sign-in before a stored model key can change. | Enrol multi-factor sign-in, then retry. |
| `mfa_invalid` | 401 | The code or recovery code is not right. | Enter the current code from your authenticator app, or an unused recovery code. |
| `mfa_not_enrolled` | 409 | Multi-factor sign-in is not on for this person. | Enrol first. |
| `mfa_required` | 403 | A stored model key change needs a fresh multi-factor step-up (within 10 minutes). | Step up with your code, then retry. |
| `mfa_required_by_policy` | 409 | The organization requires multi-factor sign-in for owners, so it cannot be turned off here. | Ask an owner to change the organization policy. |
| `mfa_unavailable` | 403,503 | This server has no readable auth key for multi-factor sign-in (or the person's factor is sealed under a key that is not loaded). | Use a recovery code, or ask the operator to fix `NURAMESH_AUTH_KEY_FILE`. |
| `mixed_currencies` | 409 | The ledger holds more than one currency, so a single total would be wrong. | Ask the operator; a cost view needs one currency. |
| `never_grantable` | 400 | This kind of action can never be granted to an agent. | Keep the action behind a human approval. |
| `no_config` | 409 | The server was started without an organization (or the run's organization is unknown). | Start the server with a configuration file that names the organization. |
| `no_content_store` | 409 | A reason is needed and the server has no content store. | Configure the content store, then retry. |
| `no_human_gate` | 409 | The task has no human gate with that id. | Use the gate id shown on the task. |
| `no_organization` | 403 | The credential does not belong to exactly one organization, so a dashboard cannot be scoped to one. | Sign in as a person or agent of a single organization. |
| `no_password_credential` | 409 | The person has no password to reset. | Nothing to reset; use the sign-in method the person has. |
| `no_pending_factor` | 409 | There is no unconfirmed factor to confirm. | Start enrolment again. |
| `no_rule` | 403 | The route has no authorization rule, so it is refused (default deny). | This is a server bug; report it. |
| `no_such_breaker` | 404 | There is no breaker with that name. | List the breakers and use one of the names. |
| `no_such_role` | 404 | There is no role with that name in the configuration. | List the roles and use one of the names. |
| `not_active` | 409 | The autonomy grant is not active (the detail names its state). | Propose a new grant if you need one. |
| `not_allowed_yet` | 403 | Stored model keys are not allowed on a hosted deployment yet (the data processing agreement is not published). | Use a key file on a self-hosted deployment, or wait for the hosted release. |
| `not_disabled` | 409 | The breaker is not disabled, so it cannot be enabled. | None; it is already on. |
| `not_floor_kind` | 400 | The model-change floor can only list kinds that may be required (not `run_command`, never-grantable kinds, or unknown ones). | Send only the kinds the `detail` allows. |
| `not_found` | 404 | No such record in your organization (a run, task, stream, approval, token, credential, agent, budget, content, connection, grant, catalog entry or scorecard subject). | Check the id and that you are signed in to the right organization. |
| `not_paused` | 409 | Two cases. A catalog entry is still approved, so it cannot be removed. Or you tried to resume a role that is not paused. | For a catalog entry, pause or retire it, then remove it. For a role, nothing: it is already running. |
| `not_proposable` | 409 | The role and action do not meet the evidence rules for a grant; the `reasons` list says which. | Collect more evidence, or fix the reasons listed. |
| `not_settable` | 400 | The setting is fixed in this version (the evidence window). | Do not change it. |
| `not_tripped` | 409 | The breaker is not tripped, so it cannot be reset. | None. |
| `not_waiting` | 409 | The task is not waiting for a human decision. | Check the task state; decide only a waiting task. |
| `org_erased` | 409 | The organization was erased (or its state could not be read, which counts as erased), so its halt cannot be lifted. | None; an erased organization stays erased. |
| `org_halted` | 409 | The organization is halted, so no connection is tested. | A person lifts the halt, then retry. |
| `org_key_destroyed` | 409 | The organization's content key was destroyed on purpose; content cannot be kept for it. Nothing was changed. | None; this is final. Start a new organization if you need to keep content. |
| `org_key_missing` | 409 | The organization has no content key but still has content (a lost key). Nothing was changed. | The operator restores the key from a backup; do not create a new one. |
| `org_key_unavailable` | 409 | The organization's ledger key is not available, so a model key cannot be fingerprinted or stored. | Ask the operator to restore the organization's key. |
| `person_disabled` | 409 | The person is disabled, so their role cannot be changed. | Enable the person first, then change the role. |
| `plan_cycle` | 422 | The plan's tasks depend on each other in a cycle (or a task depends on itself). | Remove the circular dependency and start again. |
| `plan_invalid` | 422 | The plan is not well formed. | Fix the plan against the schema and start again. |
| `plan_too_many_tasks` | 422 | The plan has more tasks than a run start accepts. | Split the plan into smaller runs. |
| `price_version_exists` | 409 | A price with this version already exists for the model. | Add the price with a new version. |
| `range_too_large` | 422 | The range holds too many ledger entries or periods to answer. | Narrow the period and ask again. |
| `rate_limited` | 429 | Too many calls or failed attempts from this client, token, credential or connection test; too many unauthenticated `GET /v1/health` calls from one client; or too many ledger verify calls (`/v1/ledger/verify`, `/v1/runs/{id}/ledger/verify`, `/v1/ledger/{stream}/verify`: 600 a minute per signed-in credential, 60 per unauthenticated client). | Wait for the `Retry-After` time (or the time in the detail) and retry. |
| `reason_required` | 422 | A first key or a revoke needs a reason of 3 to 500 characters. | Send `reason`. |
| `reauth_required` | 409 | This change needs your password again. | Enter your password and retry. |
| `resend_limit` | 409 | The invitation was resent 5 times already. | Revoke it and create a new one. |
| `retired` | 409 | The catalog entry is retired. | Remove it and add it again to use it. |
| `role_not_assignable` | 422 | The role you asked for cannot be given through the API (an owner is made only by the operator). | Ask for `viewer` or `approver`; an owner is created by the operator. |
| `run_budget_blocked` | 409 | Every agent with a budget is blocked, so the run cannot start. | A person lifts the block or raises the budget, then retry. |
| `run_budget_exhausted` | 409 | The organization's budgets leave no room to start the run. | Raise the budget or wait for the window to reset, then retry. |
| `run_budget_missing` | 409 | An agent the organization declares has no budget. | Set a budget for the agents, then retry. |
| `run_cap_reached` | 429 | The organization has too many unfinished runs and pending starts. | Wait for runs to finish (see Retry-After), then retry. |
| `run_ended` | 409 | The run has ended; a decision would change nothing. | None. |
| `run_finished` | 409 | The run has already ended; there is nothing to stop. | None. |
| `run_id_conflict` | 409 | The run id already belongs to another run or start. | Use a new idempotency key (or no key) and start again. |
| `run_rate_limited` | 429 | The organization is starting runs faster than allowed. | Slow down and retry after the Retry-After seconds. |
| `run_stopping` | 409 | The run was asked to stop; a decision would change nothing. | Wait for the run to end. |
| `same_email` | 409 | The new address is the one you already have. | Enter a different address. |
| `scorecard_unavailable` | 503 | A ledger entry the scorecard needs is unreadable. | Retry later; if it persists, the ledger needs inspection. |
| `self_approval` | 403 | You started this work (or issued the token that did), so you cannot approve it. Nothing was recorded. | Ask another person with the decide permission to approve it. |
| `session_invalid` | 401 | You are not signed in, or the session expired. | Sign in again. |
| `shipped_prices_invalid` | 503 | The price list that ships with NuraMesh failed its checks, so no price was guessed and the request was refused. | Report it to the operator: the installation is damaged or modified. Reinstall the same version, then try again. |
| `solo_rate_limited` | 429 | You approved alone too many times in the last hour (30 by default; the configuration can only lower it). Nothing was recorded. | Wait the seconds in `Retry-After`. A reject is never limited. |
| `stale_round` | 409 | The decision is for an earlier round of the gate. | Fetch the task and decide the current round. |
| `step_up_required` | 403 | A solo approval of a high-risk action needs a fresh proof: your sign-in or a password confirmation within the last 10 minutes. Nothing was recorded. | Sign in again or confirm your password (`POST /v1/auth/password/confirm`), then approve again. |
| `store_busy` | 503 | The database is busy. | Retry. |
| `store_error` | 500 | The store failed, or it holds a damaged entry, which it refuses to read past. | Retry once for a passing failure. A damaged entry stays refused until the data is repaired, so report it to the operator. |
| `template_unknown` | 422 | A task names a template that is not in the content store. | Add the template or fix its name, then start again. |
| `tls_required` | 400 | This server answers over https only. | Call it through the https proxy. |
| `token_active` | 409 | The token is still active, so it cannot be deleted. | Revoke it first, then delete it. |
| `token_invalid` | 401 | The API token or agent credential is unknown, expired or revoked, or was not sent as `Authorization: Bearer <token>`. | Send a valid token, or issue a new one. |
| `token_name_taken` | 409 | A token holds this name, or the name was used by a token that was renamed or deleted lately. | Pick another name. |
| `too_large` | 413 | The request body is over the size limit. | Send a smaller body. |
| `too_many_attempts` | 429 | Too many failed sign-in or setup-token attempts for this account, or from this client address. | Wait and try again later. |
| `unblock_refused` | 409 | The budget cannot be unblocked. The detail says why (for example the budget has no organization, or the new limit is refused). | Fix the cause in the detail, then retry. |
| `unknown_key` | 404 | This connection has no stored key. | Set a key first. |
| `unknown_version` | 404 | That key version does not exist. | Read the key state and use a listed version. |
| `unsupported_media_type` | 415 | The body is not `application/json`. | Send `Content-Type: application/json`. |
| `use_self_service` | 409 | A person turns off their own multi-factor sign-in themselves, and an owner resets their own password with the forgot-password page, not the owner route. | Use the self-service call (for multi-factor) or the forgot-password page; an owner can reset another person. |
| `user_disabled` | 409 | The person is disabled, so an owner cannot reset their multi-factor sign-in (the CLI prints `refused: user_disabled`) or their password. | Enable the person first, then retry. |
| `version_unsupported` | 400 | The `NuraMesh-Version` header is not a version this server knows (known: `2026-12`). | Send `NuraMesh-Version: 2026-12`, or leave the header out. |
| `window_too_long` | 400 | A dashboard window is longer than the limit (the detail gives the number of days). | Ask for a shorter window, or several smaller ones. |
