# Environment variables

Every `NURAMESH_*` variable the code or the Compose stack reads. NuraMesh reads no other prefix: the standard
`OTEL_*` variables are ignored on purpose (`docs/telemetry.md`). A bad value in most settings stops the process at start with one line
that names the variable and never the value. Exceptions: the `NURAMESH_OTEL_*` settings never stop the server (it
starts, logs `otel export disabled: <code>` and exports nothing). Every on/off setting
uses one grammar (below).

The fields of `nuramesh.yaml` are not environment variables: their schema is `spec/schema/nuramesh-config.schema.json`.

## Units, booleans and secrets (the freeze rules)

These rules hold for every row; an internal test checks them against this page. Names are a promise: a rename keeps the old name as a warning alias for one release; the pre-release renames of an internal decision (the two Compose-only names) have no alias.

- **Units are in the name.** A duration ends `_SECONDS`, `_MINUTES`, `_HOURS` or `_DAYS`; a size or count says what it counts in its Meaning.
- **Secrets are files, not values.** A credential is read from a file whose path is in a `*_FILE` variable (or a directory, `*_DIR`), never from the value itself. Three settings also accept the value itself (`NURAMESH_DATABASE_URL`, `NURAMESH_REFRESH_SUCCESSOR_KEY`, `NURAMESH_OTEL_HEADERS`) and each has a `_FILE` twin. Setting both the variable and its twin stops the process at start (the message names both). The file must not be readable by group or others (`chmod 600`): any group or other permission bit stops the process, and so does a missing, unreadable or empty file. One trailing newline is dropped, and a symlink to such a file is followed (Kubernetes secrets are mounted that way). The value is never printed or logged.
- **One row per name.** A name appears once; a retired name keeps one "Deprecated alias" row.

## Booleans

One grammar for every on/off setting. **On** is `1`, `true`, `yes` or `on`; **off** is `0`, `false`, `no` or `off`. Case does not matter and surrounding whitespace is ignored (`" On "` is on). An **unset** variable takes the setting's default (the Default column of the tables below; most are off, a few such as `NURAMESH_OTEL_EVENT_HASH` are on), and so does one that is **empty** or only whitespace (`NAME=`: **empty = unset**, never an error). A variable that holds anything else (`2`, `y`, `enabled`, a typo) is an error: the process stops at start with one line naming the variable (never the value). `NURAMESH_OPERATOR=true` is on. There are no per-setting exceptions and no compatibility spellings. The shell reader of the container images is stricter about embedded newlines (a value with a leading newline that Python accepts is refused there); it fails closed.

- **Stops the process at start when malformed:** `NURAMESH_HOSTED`, `NURAMESH_REQUIRE_HOSTED`, `NURAMESH_CUSTOMER_DATA_ENABLED`, `NURAMESH_TEMPORAL_TLS`, `NURAMESH_TEMPORAL_ALLOW_PLAINTEXT`, `NURAMESH_ALLOW_PRIVATE_NETWORK`, `NURAMESH_REQUIRE_DATABASE`, `NURAMESH_OPERATOR`, `NURAMESH_KEYS_DPA_PUBLISHED`, `NURAMESH_SMTP_ALLOW_INSECURE`. (`serve` and `worker` check all ten before they start; a command that reads one refuses with the same line.)
- **Same grammar, but a malformed value disables the exporter and never stops the server (`otel export disabled: bad_setting`):** `NURAMESH_OTEL_ALLOW_INSECURE`, `NURAMESH_OTEL_EVENT_HASH`, `NURAMESH_OTEL_COST_METRIC`, `NURAMESH_OTEL_GENAI_ATTRS`.

"Secret" means the value is a credential: keep it out of Git, images and logs (`docs/self-hosting.md` keeps secrets as
files). A test fails when the code or `deploy/compose/` reads a variable this page
does not list, or this page lists one the code no longer reads. Variables for tools that are not built are not listed.

## Store and keys

| Variable | Default | Meaning | Secret | Read by |
|---|---|---|---|---|
| `NURAMESH_DATABASE_URL` | unset: the team's SQLite file | PostgreSQL URL; when set, `serve`, `worker` and the store commands use PostgreSQL (not together with the `_FILE` twin) | yes | CLI, server |
| `NURAMESH_DATABASE_URL_FILE` | unset | path of a file (mode `0600`, `0400`) holding the PostgreSQL URL; the secret-file rules above apply | no (the file is) | CLI, server |
| `NURAMESH_MIGRATE_ACK` | none, no default | the highest pending migration number; acknowledges that the backup of `docs/upgrade-notes.md` exists, which `nuramesh db migrate` requires while a not-additive migration is pending (issue 897); set it for one run, then clear it | no | CLI (`db migrate`), Compose `migrate` service |
| `NURAMESH_PG_PROVIDER_ROLE` | none, no default | the managed-PostgreSQL provider's bootstrap superuser role (for example `azuresu` on Azure Flexible Server, `rdsadmin` on AWS RDS) whose ADMIN-only row on `nuramesh_definer` is allowed; unset means such a row refuses the start; never set it on self-hosted PostgreSQL | no | CLI, server, worker |
| `NURAMESH_LIGHT_WORKERS` | `4` | Runs the light engine works on at once (1 to 32; a larger value is refused at start) | no | light runner |
| `NURAMESH_SHUTDOWN_GRACE_SECONDS` | `30` | Seconds a light runner waits for steps in flight when it is told to stop; the rest are re-run on the next start | no | light runner |
| `NURAMESH_RUN_START_TTL_SECONDS` | `3600` | Seconds a start request may wait for a runner; an older one ends refused with `run_start_expired` | no | light runner, server |
| `NURAMESH_RUN_ENGINE` | `auto` | `auto`, `light` or `temporal`: which engine runs the runs started by `POST /v1/runs`. The setting works today; resuming a run after a crash works on the light engine; the Temporal production profile is not shipped yet. `auto` is Temporal when `NURAMESH_TEMPORAL_ADDRESS` is set, else light; a hosted deployment accepts Temporal only (`nuramesh serve` exits 2 otherwise) | no | server |
| `NURAMESH_SHUTDOWN_GRACE` | unset | Deprecated alias of `NURAMESH_SHUTDOWN_GRACE_SECONDS`: read for one more release with a warning in the log; when both are set the new name wins | no | same as the new name |
| `NURAMESH_RUN_START_TTL` | unset | Deprecated alias of `NURAMESH_RUN_START_TTL_SECONDS`: read for one more release with a warning in the log; when both are set the new name wins | no | same as the new name |
| `NURAMESH_RUN_START_RATE` | `60` | Run starts a minute for one organization (429 `run_rate_limited` above it) | no | server |
| `NURAMESH_MAX_ACTIVE_RUNS` | `50` | Unfinished runs plus pending start requests for one organization (429 `run_cap_reached` at it) | no | server |
| `NURAMESH_CUSTOMER_DATA_ENABLED` | unset | on lets `POST /v1/runs` accept `data_class: customer`; the operator turns it on only after a lawful basis for the data is in place; off or unset refuses it; a garbage value stops the runner at start. The light runner reads its own copy: without it a plan with a real model and any data class but `synthetic` is refused `customer_data_not_enabled` | no | server, worker |
| `NURAMESH_REQUIRE_DATABASE` | unset | on makes a missing `NURAMESH_DATABASE_URL` (CLI) or a missing `NURAMESH_DB_PASSWORD_FILE` (`nuramesh-env`) an error instead of a SQLite or unchanged-command fallback; set in every Compose service | no | CLI, `nuramesh-env` |
| `NURAMESH_DB_HOST` | none | Compose: the database host; `nuramesh-env` builds the URL from the `NURAMESH_DB_*` variables | no | `nuramesh-env` |
| `NURAMESH_DB_PORT` | `5432` | Compose: the database port | no | `nuramesh-env` |
| `NURAMESH_DB_NAME` | `nuramesh` (Compose `.env`) | Compose: the database name | no | `nuramesh-env`, Compose |
| `NURAMESH_DB_USER` | none | Compose: the role (`nuramesh_owner` for `migrate`, `nuramesh_app` for services) | no | `nuramesh-env` |
| `NURAMESH_DB_PASSWORD_FILE` | none | Compose: path of the file holding that role's password | no (the file is) | `nuramesh-env` |
| `NURAMESH_KEY_DIR` | `~/.nuramesh/keys` (`nuramesh worker` has no default: it refuses to start without it) | where organization content keys, tombstone files and the OTel pseudonym key live; one folder per store inside | no (the folder is) | CLI, server, worker |
| `NURAMESH_TEAM_DIR` | unset | the team folder for `nuramesh worker` and for every organization command (`nuramesh org ...`, R1.43) when no TEAM argument is given; set it and all of them follow | no | CLI, worker |
| `NURAMESH_COMPOSE_TEAM_PATH` | none (required) | Compose only (the same folder as `NURAMESH_TEAM_DIR` inside the containers): absolute host path of the team folder, mounted read-only at `/data/team` | no | Compose |
| `NURAMESH_COMPOSE_IMAGE_TAG` | `local` | Compose only: the tag of the locally built `nuramesh-*` images | no | Compose |
| `NURAMESH_ANCHOR_SCHEMA` | `public` | the database schema that holds the anchor function (`--schema` overrides) | no | minter commands |
| `NURAMESH_ANCHOR_SSH_KEY` | unset | the PATH of the SSH deploy key file used by `nuramesh anchor export --push` to push the `ledger-anchors` branch | no (the file is) | anchor export |
| `NURAMESH_OPERATOR` | unset | on marks the owner shell; `--new-epoch` on the token minter and `auth invites create ... --print-link` need it | no | minter commands |
| `NURAMESH_AUTH_KEY_FILE` | unset: MFA enrolment off | path of a `0400`/`0600` file holding the 32-byte auth key (base64, one line) that seals TOTP secrets; never the key itself | no (the file is) | R1.38 MFA; runbook |
| `NURAMESH_AUTH_KEY_FILE_PREVIOUS` | unset | the previous auth key during a rotation (opens old secrets only, never seals) | no (the file is) | R1.38 MFA; runbook |
| `NURAMESH_MFA_ISSUER` | `NuraMesh` | the name an authenticator app shows for the account (printable, at most 64 characters) | no | MFA |
| `NURAMESH_MFA_CHALLENGE_SECONDS` | `300` | how long the second step of a login may take after the password (30 to 900; an enrolment step never exceeds 300) | no | MFA |
| `NURAMESH_INVITE_TTL_HOURS` | `72` | invite lifetime in hours, 1 to 168 (R1.37) | no | server |
| `NURAMESH_RESET_TTL_MINUTES` | `60` | password reset link lifetime in minutes, 5 to 1440 (R1.41) | no | server |
| `NURAMESH_INVITE_MAX_PENDING` | `100` | pending invites per organization, 1 to 1000 (R1.37) | no | server |
| `NURAMESH_EMAIL_VERIFY_TTL_HOURS` | `24` | life of an email change link in hours, 1 to 168 (R1.40); the link is `<URL>/verify-email#t=<token>` | no | server |

**Switching the run engine is not supported in this release.** Do not change `NURAMESH_RUN_ENGINE` (light <-> temporal) while runs are unfinished. A run belongs to the engine that started it; after a switch the new engine does not pick it up, so the run is stranded (it stays unfinished and does nothing). There is no startup check that warns you. Finish or stop all runs first, then switch. There is also no supported way to move runs between engines yet.

## Server and sessions

| Variable | Default | Meaning | Secret | Read by |
|---|---|---|---|---|
| `NURAMESH_HOSTED` | unset (not hosted) | on = hosted deployment (caps content retention at 90 days, where self-hosted allows up to 396); unset or off = not hosted; empty counts as unset; any other value is an error. While `1` (or unreadable) the prompt-cache switch is refused (API 403 `hosted_refused`, CLI exit 3) and caching stays off whatever the ledger holds | no | CLI, server |
| `NURAMESH_REQUIRE_HOSTED` | unset | on makes the process refuse to start unless `NURAMESH_HOSTED` is also on; off or unset = no requirement | no | server |
| `NURAMESH_REFRESH_GRACE_SECONDS` | `30` (`0` on a loopback server with no proxy and no key) | refresh grace window, whole seconds 0 to 30; `0` turns it off | no | server |
| `NURAMESH_REFRESH_SUCCESSOR_KEY` | unset | at least 32 random bytes, base64url; required while the grace window is above 0 (not on a loopback server with no trusted proxy and the window unset: there the window is off) | yes | server |
| `NURAMESH_REFRESH_SUCCESSOR_KEY_FILE` | unset | path of a file (mode `0600`, `0400`) holding that key (base64url, one line); the secret-file rules above apply; not together with the plain variable. In the Compose stack the `nuramesh-env` wrapper of the images reads it instead (a `0644` Compose secret, mode not checked; shape checked, exit 2 on a bad file) and hands the server the value | no (the file is) | server; Compose: `nuramesh-env` |
| `NURAMESH_DECISION_SWEEP_SECONDS` | `300` | seconds between sweeps that reconcile unmarked human decisions; a positive whole number | no | server |
| `NURAMESH_TRUSTED_PROXIES` | unset | comma-separated CIDRs or addresses of the reverse proxies whose `X-Forwarded-*` headers are believed; required by `serve --trusted-proxy`, which refuses to start without it (and a list without the flag refuses too); `/0` and malformed entries refuse | no | server |
| `NURAMESH_CONTENT_EXPIRY_SECONDS` | `3600` | seconds between runs that delete expired stored content on the API server; a whole number, `0` turns it off | no | server |
| `NURAMESH_ALLOW_PRIVATE_NETWORK` | unset | on or off; on lets outbound model calls reach private, loopback and link-local hosts (refused by default); a process honours it only after it has written the ledger entry `network.private_allowed` (the server at start, and `run`, `eval`, `worker`, `models` (not the read-only `catalog`, `assignments` and `connections`) and `calibrate` when they open the store); needed for a local model on `http://127.0.0.1` | no | CLI, server, worker |
| `NURAMESH_AGENT_CREDENTIAL_DAYS` | `30` | default lifetime of an agent credential, 1 to 90 days | no | server |
| `NURAMESH_AGENT_CREDENTIAL_MAX_LIVE` | `5` | live credentials per agent, 1 to 20 | no | server |
| `NURAMESH_AGENT_CREDENTIAL_CALLS_PER_MINUTE` | `600` | valid calls a minute per credential, 10 to 6000 | no | server |
| `NURAMESH_URL` | unset | the API server a standalone agent binds to with `nuramesh.bind(team, role)` when `server=` is not given; `https://`, or `http://` only for a loopback host | no | Python library (`nuramesh.bind`) |
| `NURAMESH_AGENT_CREDENTIAL` | unset | the agent credential (`nma_...`) `nuramesh.bind` sends; a person's token or a session value is refused; never passed as a parameter | yes | Python library (`nuramesh.bind`) |
| `NURAMESH_AGENT_CREDENTIAL_FILE` | unset | a file holding the agent credential on one line, in place of `NURAMESH_AGENT_CREDENTIAL`; setting both is refused | no (a path) | Python library (`nuramesh.bind`) |
| `NURAMESH_AGENT_USER` | `nuramesh-agent` | name of the coding agent's operating-system account; a key file owned by this account is refused (letters, digits, `_` and `-`, at most 32, starting with a letter or `_`; an invalid value uses the default and logs a warning) | no | CLI, server |
| `NURAMESH_DASHBOARD_MAX_EVENTS` | `1000000` | ledger entries one dashboard read may scan; past it the answer is `partial: true` with `scanned_events`; a whole number of 1 or more, otherwise start-up stops | no | server |
| `NURAMESH_DASHBOARD_MAX_CONCURRENCY` | `2` | dashboard reads computed at the same time per server; a whole number of 1 or more, otherwise start-up stops (`docs/api.md`) | no | server |

## Worker and models

| Variable | Default | Meaning | Secret | Read by |
|---|---|---|---|---|
| `NURAMESH_TEMPORAL_ADDRESS` | unset | host:port of Temporal; `nuramesh worker` needs it, and when set the server wakes a person's waiting task through Temporal (needs `nuramesh[temporal]`) | no | server, worker |
| `NURAMESH_TEMPORAL_TLS` | unset | on turns TLS on for the Temporal connection; required with credentials when the address is not loopback (otherwise `serve`, `worker` and the waker refuse, failing closed) | no | server, worker |
| `NURAMESH_TEMPORAL_TLS_CA_FILE` | unset | path of a private CA certificate for the Temporal server | no (the file is public) | server, worker |
| `NURAMESH_TEMPORAL_TLS_CERT_FILE` | unset | path of the client certificate for mutual TLS (used with the key file) | no | server, worker |
| `NURAMESH_TEMPORAL_TLS_KEY_FILE` | unset | path of the client private key for mutual TLS | yes (the file is) | server, worker |
| `NURAMESH_TEMPORAL_TLS_SERVER_NAME` | unset | the server name to check in the Temporal certificate when it differs from the address | no | server, worker |
| `NURAMESH_TEMPORAL_API_KEY` | unset | an API key for Temporal (an alternative to a client certificate) | yes | server, worker |
| `NURAMESH_TEMPORAL_API_KEY_FILE` | unset | path of a file holding the Temporal API key | no (the file is) | server, worker |
| `NURAMESH_TEMPORAL_ALLOW_PLAINTEXT` | unset | on lets a non-loopback Temporal address be reached without TLS (the Compose dev stack only; never with an API key); the process first writes the ledger entry `network.temporal_plaintext_allowed` and refuses to connect if it cannot | no | server, worker |
| `NURAMESH_TEMPORAL_NAMESPACE` | `default` | the Temporal namespace | no | server, worker |
| `NURAMESH_TEMPORAL_TASK_QUEUE` | `nuramesh` | the Temporal task queue | no | worker |
| `NURAMESH_WORKER_CONNECT_TIMEOUT_SECONDS` | `120` | Seconds the worker keeps retrying its first Temporal connection (with backoff) before it exits; a bad value exits 2 | no | worker |
| `NURAMESH_WORKER_CONNECT_TIMEOUT` | unset | Deprecated alias of `NURAMESH_WORKER_CONNECT_TIMEOUT_SECONDS`: read for one more release with a warning in the log; when both are set the new name wins | no | same as the new name |
| `NURAMESH_WORKER_HEALTH_PORT` | unset | a port number: the worker answers a plain health reply on loopback there (ready only once connected); a busy or invalid port exits 2 with a coded one-line error | no | worker |
| `NURAMESH_ANTHROPIC_KEY_FILE` | unset | path of a `0400` or `0600` file holding the Anthropic API key, outside every repository; self-hosted only (the key is never read from the environment) | no (the file is) | model provider |
| `NURAMESH_SECRET_DIR` | unset | directory of the managed file secret store for stored model keys: absolute, outside every repository, mode `0700`, one `0600` file per key version; self-hosted only (the file store is refused when `NURAMESH_HOSTED=1`); the key value is never read from the environment; the light runner without `--fake` reads stored keys from it at the call and never writes to it, a `--fake` runner and the Temporal worker never open it | no (the directory holds secrets) | secret store |
| `NURAMESH_API_URL` | `http://127.0.0.1:8765` | the server the `nuramesh models key` commands talks to, when `--server` is not given: https, or http to this machine only, no login or path in it; the owner's login and the key travel only to this address | no (a URL, never a credential) | CLI |
| `NURAMESH_SECRET_STORE` | unset | which secret store holds stored model keys: unset uses the managed file store when `NURAMESH_SECRET_DIR` is set, else no store (stored keys off); `file` is the file store (`NURAMESH_SECRET_DIR` required); `fake` is an in-memory store for local trials (keys vanish with the process); any other name is a store an installed plugin registered (the Azure Key Vault adapter is `azure-keyvault`, from the commercial plugin, which also reads its own vault settings). A hosted deployment (`NURAMESH_HOSTED=1`) must name a plugin store: `fake`, `file` and unset are refused at start; a plugin name together with `NURAMESH_SECRET_DIR` is refused | no (a name, never a key) | secret store |
| `NURAMESH_KEY_SWEEP_SECONDS` | `60` | seconds between runs of the stored-key sweeper on the API server (first run at start); a whole number, `0` turns the periodic run off (the shred hook stays); only used when `NURAMESH_SECRET_DIR` is set | no | server |
| `NURAMESH_KEY_CACHE_SECONDS` | `0` | how long a process may keep a stored model key it has read: `0` = off (every call reads the store), at most `60`; a revoke or an ended grace is always checked in the ledger first and drops the cached value; not read by the server yet: the setting has no effect today | no | secret store |
| `NURAMESH_KEYS_DPA_PUBLISHED` | unset | set to on once the data processing agreement is published: only then may a hosted owner set or rotate a stored model key (revoke is never gated by it); self-hosted ignores it | no | key routes |
| `NURAMESH_LICENCE_FILE` | unset: no licence, every open feature stays on | path to one signed licence file (`{"claims": ..., "sig": ...}`) checked offline against the public keys pinned in code (the table is empty until the production key exists); a missing, unreadable, too large, malformed, expired or unverifiable file, a missing `cryptography` package (`nuramesh[server]`) or any hosted deployment (`NURAMESH_HOSTED=1`: refused, the hosted layer supplies entitlements) prints one `licence: <problem>: <detail>` line on stderr, grants nothing and never changes an open feature; no network check | no | `serve` and plugin commands (the plugin loader) |
| `NURAMESH_OPENAI_COMPATIBLE_KEYS` | unset: the OpenAI-compatible provider has no key bindings and every call is refused | path of the operator's key-binding file for the OpenAI-compatible provider (a small file, at most 16 KiB, that binds each key name to a key file); read fresh on every call, never cached; an absolute path outside every repository, with the same file checks as other key files (a bad or unreadable file is refused with a text that never names the path); self-hosted only | no (the file is) | model provider |
| `NURAMESH_HALT_REFUSAL_WINDOW_SECONDS` | `3600` | the window in which repeated starts refused under a halted (erased) organization are coalesced into one summary ledger entry; a whole number from 1 to 86400, anything else is refused (fail closed) | no | engine |
| `NURAMESH_LOCAL_MODEL` | `qwen3:8b` | Compose `local-model` profile: the Ollama model tag that is pulled and verified (for example `qwen3:4b` on a low-RAM host) | no | Compose |
| `NURAMESH_LOCAL_MODEL_PROCESSING` | unset | Compose `local-model` profile: the host region for the local model connection (`canada`, `global` for synthetic data only, or `region:<id>`); never guessed (`docs/self-hosting.md`) | no | Compose, server |
| `NURAMESH_SANDBOX_SOCKET` | unset | path of the sandbox service's Unix socket (Compose: `/run/sandbox/sandbox.sock`); set and not empty, the runner runs gate commands through the sandbox service; read by the `runner` and by the `sandbox` service, which listens there | no | light runner, sandbox service |
| `NURAMESH_SANDBOX_UID` | unset | the only user id the runner accepts as the peer on the sandbox socket (Compose: `10002`); unset, no peer passes | no | light runner |
| `NURAMESH_SANDBOX_CLIENT_UID` | unset | the only user id the sandbox service answers on its socket (Compose: `10001`, the runner's user) | no | sandbox service |
| `NURAMESH_SANDBOX_WORKSPACE_MAX_MB` | `256` (Compose) | largest workspace, in MiB, the sandbox service copies for one command | no | sandbox service |
| `NURAMESH_SANDBOX_BWRAP` | `bwrap` | command line of the bubblewrap program the sandbox service uses; changed only for tests | no | sandbox service |
| `NURAMESH_SANDBOX_WORK` | `/work` | folder for the workspace copies (memory-backed in Compose) | no | sandbox service |
| `NURAMESH_SANDBOX_IMAGES` | `/images` | colon-separated folders holding the unpacked images (Compose: the read-only `sandbox-images` volume) | no | sandbox service |
| `NURAMESH_SANDBOX_SELFTEST_ROOTFS` | `/opt/nuramesh/selftest-rootfs` | root folder used by the sandbox service's start-up self-test | no | sandbox service |
| `NURAMESH_SANDBOX_CGROUP` | `/sys/fs/cgroup` | cgroup folder the sandbox service reads its limits from; changed only for tests | no | sandbox service |
| `NURAMESH_SANDBOX_PROC_SELF` | `/proc/self` | path used to inspect the sandbox service's own process; changed only for tests | no | sandbox service |

Licence grace: a licence keeps granting for 30 days after its `expires_at`; only a file expired past that grace is refused.

## Email (R1.39)

Email is off while `NURAMESH_SMTP_HOST` is unset (the server starts; every feature that needs mail answers 503 `email_not_configured`). With the host set, a bad value stops the server at start (the message names the variable, never the value). `nuramesh email status` and `nuramesh email check --to ADDRESS` check the setup. Mail goes through an SMTP server you choose, which is a subprocessor: it sees every recipient and every link.

| Variable | Default | Meaning | Secret | Read by |
|---|---|---|---|---|
| `NURAMESH_SMTP_HOST` | unset: email off | SMTP server host | no | server, CLI |
| `NURAMESH_SMTP_PORT` | by security: `587` (`starttls`), `465` (`tls`), `25` (`none`) | 1 to 65535 | no | server, CLI |
| `NURAMESH_SMTP_SECURITY` | `starttls` | `starttls`, `tls` (implicit) or `none`; `none` only to `localhost`, `127.0.0.1` or `::1`, or with `NURAMESH_SMTP_ALLOW_INSECURE=1` for a relay without login | no | server, CLI |
| `NURAMESH_SMTP_ALLOW_INSECURE` | unset | on permits `none` to a non-loopback host (never together with `NURAMESH_SMTP_USER`) | no | server, CLI |
| `NURAMESH_SMTP_USER` | unset | login name (needs the password file) | no | server, CLI |
| `NURAMESH_SMTP_PASSWORD_FILE` | unset | path of a regular file (not a symlink) owned by the server's user, mode `0400` or `0600`, at most 1 KiB, outside any repository, holding the password on one line, ASCII only (a non-ASCII password is refused at start-up and at send time); read at every send | no (the file is) | server, CLI |
| `NURAMESH_SMTP_FROM` | unset | sender address (required when the host is set; ASCII) | no | server, CLI |
| `NURAMESH_SMTP_TIMEOUT_SECONDS` | `10` | connect and read timeout, 1 to 60 | no | server, CLI |
| `NURAMESH_MAIL_QUEUE_MAX` | `100` | background mails on the main queue (everything except email-change verification links) queued, being sent or reserved, 2 to 1000; an email change reserves 1 here for its notice | no | server |
| `NURAMESH_VERIFY_MAIL_QUEUE_MAX` | `100` | capacity of the separate verification-mail queue (own worker; only the link mail of an email change uses it, so it never delays or fills the main queue), 8 to 1000; an email change reserves 1 here | no | server |
| `NURAMESH_PUBLIC_URL` | unset | the base URL people open: `https://...`, or `http://` for `localhost`/loopback only; no userinfo, path, query or fragment (required when the host is set) | no | server, CLI |
| `NURAMESH_ORG_NAME` | unset: `your organization` | organization name used in mails, 1 to 80 characters, no control or format characters | no | server, CLI |

## OpenTelemetry export

Off unless `NURAMESH_OTEL_ENDPOINT` is set. Details and the privacy rules: `docs/telemetry.md`.

| Variable | Default | Meaning | Secret | Read by |
|---|---|---|---|---|
| `NURAMESH_OTEL_ENDPOINT` | unset: off | OTLP/HTTP base URL (https, or http to localhost) | no | exporter |
| `NURAMESH_OTEL_HEADERS` | none | `name=value,...` request headers (not together with the `_FILE` twin) | yes | exporter |
| `NURAMESH_OTEL_HEADERS_FILE` | none | path of a file (mode `0600`, `0400`) holding those headers on one line; the secret-file rules above apply (a bad file disables the exporter, it never stops the server) | no (the file is) | exporter |
| `NURAMESH_OTEL_ALLOW_INSECURE` | `false` | allow `http://` to a non-local host | no | exporter |
| `NURAMESH_OTEL_SIGNALS` | `traces,metrics` | which signals to export | no | exporter |
| `NURAMESH_OTEL_SAMPLE_RATIO` | `1` | trace sampling, 0 to 1 | no | exporter |
| `NURAMESH_OTEL_PERSON_IDS` | `none` | `none` or `pseudonym` | no | exporter |
| `NURAMESH_OTEL_PSEUDONYM_KEY_FILE` | `<key dir>/otel-pseudonym.key` | the pseudonym key file (mode 0600) | no (the file is) | exporter |
| `NURAMESH_OTEL_EVENT_HASH` | `true` | add the ledger entry hash as an attribute | no | exporter |
| `NURAMESH_OTEL_COST_METRIC` | `false` | export the cost counter | no | exporter |
| `NURAMESH_OTEL_GENAI_ATTRS` | `false` | also add `gen_ai.*` attributes | no | exporter |
| `NURAMESH_OTEL_BATCH_SIZE` | `256` | events per batch, 1 to 4096 | no | exporter |
| `NURAMESH_OTEL_POLL_SECONDS` | `2` | seconds between reads of the ledger, 1 to 300 | no | exporter |
| `NURAMESH_OTEL_EXPORT_TIMEOUT_SECONDS` | `10` | per request, 1 to 120 | no | exporter |
| `NURAMESH_OTEL_MAX_LAG_EVENTS` | `100000` | events behind before the exporter skips ahead and counts the gap | no | exporter |
| `NURAMESH_OTEL_STATE_FILE` | `<key dir>/otel-cursors.json` | per-stream cursors | no | exporter |
| `NURAMESH_OTEL_DEPLOYMENT` | unset | optional `nuramesh.deployment` resource attribute | no | exporter |

## Not passed into sandboxes

Any name starting `NURAMESH_` is refused as a sandbox environment variable (`docs/sandbox.md`), so these settings can
never be handed to code an agent runs.
