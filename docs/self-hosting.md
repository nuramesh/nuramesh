# Self-hosting with Docker Compose

**Docker Compose self-hosting is coming soon.** It is not yet a supported way to run NuraMesh, and the steps below are a preview for evaluation, not a supported install. To run NuraMesh today, follow the Quickstart guide.

The Docker Compose stack is designed to run one NuraMesh for a small team on one machine: the API server, the light-engine `runner`, PostgreSQL and the web app. Use synthetic data only.

You need Docker Engine with Compose v2. The stack publishes one port, the web app's TLS listener on `127.0.0.1:8443`; the API is never published.

## Start the stack

```bash
sh deploy/compose/init-secrets.sh                      # generates secrets/ (never committed)
cp deploy/compose/env.example deploy/compose/.env      # then edit NURAMESH_COMPOSE_TEAM_PATH
sh scripts/images/build.sh api worker && sh deploy/compose/build-web.sh
docker compose -f deploy/compose/compose.yml up -d --wait
```

The `nuramesh-*` images are built on your machine and never pulled. Open `https://127.0.0.1:8443`. The certificate is self-signed, so your browser warns once.

## First owner setup

A new install has no users. Create the first owner with a one-time setup token (valid 24 hours, works once) and paste it on the app's setup page, or open `/setup#token=<token>` on the address above. See [first-run.md](first-run.md) for how the token is made and what to do if it expired.

After the first sign-in, turn on two-step sign-in (MFA) for the owner account: see the two-step sign-in guide.

## The runner

Runs started from the app or the API execute on the built-in light engine, the `runner` service. It is the intended default for a small team's server. Its limits: one runner per database (never scale it), no automatic failover (a standby takes over only after the 60 s lease), and nothing runs while the server is down. `GET /v1/health` names the engine. Temporal is an opt-in profile for evaluation only. The runner uses the fake model until the sandbox work lands, so use synthetic data only.

## Sandbox image and a rootless container engine

Test-command gates run in a sandbox container (see the Run in a sandbox guide). The default stack requires two things on the host:

- the sandbox image, pinned by digest and pulled before the first run, and
- a rootless container engine (rootless Podman or rootless Docker), so a sandbox escape is an unprivileged user on the host, not root.

Without them a test-command gate refuses to run; it never falls back to your machine. `nuramesh doctor` checks the machine and prints one line per check (`PASS`, `WARN` or `FAIL`) with a `fix:` hint; today its container check reports whether a Podman or Docker runtime is present. The check that fails closed when the sandbox image or the rootless engine is missing, and prints the exact fix command, is being added to `nuramesh doctor`; until it lands, check both yourself with `docker info` (look for `rootless` in the security options) or `podman info`. See the CLI reference for every check.

## Backup, restore and upgrades

See the Store maintenance guide for backup and restore, and the Upgrade notes before you upgrade.
