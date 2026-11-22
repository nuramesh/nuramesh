# Container images

Three images, built locally from the built wheel and the locked, hash-pinned dependencies. Nothing
here pushes to a registry or touches Azure.

| Image | File | Extras | Default command |
| --- | --- | --- | --- |
| `nuramesh-api` | `scripts/images/api.Dockerfile` | yaml, server, postgres, content, azure | `serve /data/team --host 127.0.0.1 --port 8765` |
| `nuramesh-worker` | `scripts/images/worker.Dockerfile` | yaml, temporal, postgres, langgraph, content, azure | `worker --fake` (refuses to start without `NURAMESH_TEMPORAL_ADDRESS` and `NURAMESH_TEAM_DIR`) |
| `nuramesh-anchor` | `scripts/images/anchor.Dockerfile` | yaml, anchors, postgres, azure (+ git, ssh) | `anchor --help` |

## Build and run locally

```bash
sh scripts/images/build.sh            # all three; or: sh scripts/images/build.sh api
docker run --rm -v nmdata:/data --entrypoint nuramesh nuramesh-api:local init /data/team
docker run --rm --read-only --tmpfs /tmp -v nmdata:/data nuramesh-api:local
```

**The API is not reachable from outside its container yet.** The default command listens on
127.0.0.1 *inside* the container, so `-p 8765:8765` (with any host address) reaches nothing, and
`nuramesh serve` refuses any other host until R1.22 (authentication, trusted proxy, allowed hosts)
exists. Check it from inside: `docker exec <container> python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8765/v1/health').status)"`.

`build.sh` runs `uv build --wheel` and `uv export --frozen --no-dev` (with hashes) into
`build/images/`, then `docker build`. The images install with `pip --require-hashes --no-deps`, so
the Python packages are exactly the locked, hash-checked ones (the pinned base image brings its own
pip and the Debian packages, and the anchor image adds `git` and `openssh-client`, pinned as
below); no dev or test dependencies, no pip cache, no build tools in the
runtime stage. `pip-audit` on the three exported files reports no known vulnerabilities (re-run:
`uv run pip-audit -r build/images/api.requirements.txt --no-deps --disable-pip`). For an SBOM,
point syft or `docker sbom` at the image, or read the hash-pinned requirements files.

The virtual environment is created `--without-pip`, so `/opt/venv` carries no pip (the base
image's pip installs into it with `--python`), and the runtime stage then uninstalls the base image's
pip, setuptools and wheel (`pip uninstall`), so the running image has no pip command. (Python's
`ensurepip` module still ships a bundled pip wheel, as in the base image, so this makes installing
harder, not impossible; `apt` and `dpkg` remain too; the user is non-root and the filesystem can run read-only.)

**Not done yet: vulnerability scanning of the images (Trivy or Grype) and image signing
(cosign).** Both are P1 in an internal document; until they exist, `pip-audit` on the
requirements files above is the only scan.

### The anchor image's `git` and `openssh-client` are pinned by snapshot

`apt` in the anchor image reads Debian's snapshot archive at one timestamp
(`snapshot.debian.org/archive/debian/20261020T213255Z`, and `debian-security` at the same time),
the timestamp the pinned base image itself was built from (its
`/etc/apt/sources.list.d/debian.sources` names it), so the same package versions come out on
every build (today `git 1:2.39.5-0+deb12u3`, `openssh-client 1:9.2p1-2+deb12u10`). Packages are still
verified against Debian's archive keyring (`Signed-By`); `Check-Valid-Until=false` is needed only
because old snapshot indexes are past their validity date. The timestamp is also
recorded in `scripts/images/base-snapshot.txt`, and a test fails if the Dockerfile differs from it or
it lies in the future. To refresh: `sh scripts/images/update-digest.sh` pulls the base, reads its
snapshot timestamp, and updates both the file and the two URIs in `anchor.Dockerfile`; rebuild and check
`docker run --rm --entrypoint dpkg-query nuramesh-anchor:local -W git openssh-client`.
`tests/test_dockerfiles.py` fails on an `apt-get install` that is neither `package=version` nor
read from a timestamped snapshot source, and on any apt source other than that one file, any URL
host other than `snapshot.debian.org`, or an apt trust switch (`Trusted: yes`,
`--allow-unauthenticated`, `AllowInsecureRepositories`).

**Trade-offs of the snapshot.** (1) Availability: a build needs `snapshot.debian.org`, a volunteer
service that can be slow or down; a failed build is retried, never silently switched to the live
archive. (2) Frozen security fixes: git and ssh stay at the snapshot's versions until someone
refreshes it, so refresh with every base-image update (and when Debian publishes a fix for either
package). (3) `Check-Valid-Until=false` turns off apt's expiry check on the index, which snapshots
always fail; integrity is still checked against the signed `Release` file and the archive keyring.

The base is `python:3.12-slim-bookworm` pinned by digest (the multi-arch index). Refresh with
`sh scripts/images/update-digest.sh`, rebuild, review the diff. `tests/test_dockerfiles.py` fails on
an unpinned `FROM` or `# syntax=` line, an unpinned `apt-get install`, a `pip install` that is
neither hash-checked nor the local wheel, a final user of root (or `root:group`), a non-loopback
default API host, an `!` rule in `.dockerignore` beyond `build/images/*.requirements.txt` and
`build/images/wheel/*.whl`, missing healthcheck, remote `ADD`, secret-looking `ARG`/`ENV`, or a
`.dockerignore` that lets sensitive paths in. Set `NURAMESH_IMAGE_SMOKE=1` to also build the API
image and run it read-only.

## Runtime rules

- User `10001`, no shell login, `WORKDIR /data` (the only writable place besides `/tmp`); run with
  `--read-only --tmpfs /tmp`. Compiled bytecode is baked in, so no writes to the image are needed.
- No secrets in the image or Git: pass `NURAMESH_*` settings, `NURAMESH_DATABASE_URL` (from Key
  Vault in staging) and `AZURE_CLIENT_ID` at run time.
- API healthcheck: `GET /v1/health` on 127.0.0.1:8765. Worker and job images can only prove the
  install imports (no HTTP port).
- The `.dockerignore` denies everything except `build/images/`.

## What staging needs (open points)

1. **The API cannot listen on a network address yet.** `nuramesh serve` refuses any non-loopback
   host until R1.22 (authentication, trusted proxy, allowed hosts) exists, so the image's default
   command is loopback and is not reachable through Container Apps ingress. When R1.22 lands,
   staging sets `--host 0.0.0.0` (command args in `infra/staging/main.tf`) together with its auth,
   trusted-proxy and allowed-host settings; the image default must keep refusing without them.
2. **The worker command exists** (`nuramesh worker --fake`, extra `nuramesh[temporal]`): it reads
   `NURAMESH_TEMPORAL_ADDRESS`, `NURAMESH_TEMPORAL_NAMESPACE` (default `default`),
   `NURAMESH_TEMPORAL_TASK_QUEUE` (default `nuramesh`), `NURAMESH_TEAM_DIR` and, for the store,
   `NURAMESH_DATABASE_URL` (a secret: environment only), stops gracefully on SIGTERM/SIGINT, and exits 1
   with a plain message if it cannot connect. A Temporal address that is not loopback needs TLS and
   credentials (`NURAMESH_TEMPORAL_TLS=1` plus a client certificate or API key; see
   `docs/quickstart-temporal.md`), else it exits 2 before connecting. It runs on the team's fake model until real providers
   are approved (`--fake` is required, the same rule as `run`), and builds each engine from one team
   folder. The factory gets the organization id and the run id of the activity, so `env.run_id` in
   `setup` is the real run id; `setup` runs **once per run in a worker process** (the last 32 runs
   are cached), not once per activity, so a team's state is shared by one run's tasks inside a
   process but not across replicas or restarts. **Limit:** a run dropped from the cache is only
   forgotten; scratch folders or exit handlers its `setup` created stay until the process ends
   (a team has no teardown hook). The team is also set up once at start as a check (exit 2 if
   it fails or is not on the fake model).
   **The worker has no sandbox, and none is built:** a `test_command` gate fails with "the worker
   has no sandbox: command execution is refused in hosted mode" and the task goes to a
   human. **Hosted operators set `NURAMESH_HOSTED=1`** (opt-in; unset, empty or `0` is self-hosted):
   the worker then caps content retention at 90 days and refuses to start with a larger
   `content.retention_days` (exit 2). **`NURAMESH_KEY_DIR` is required** (the worker refuses to start without it, exit 2):
   content keys live there, so it must be a mounted volume, one shared directory for every
   replica; a worker restarted with an empty or per-container directory cannot read content it
   stored before. With `NURAMESH_DATABASE_URL` set, SQLite is never opened, so the team folder can
   be read-only (the same holds for `nuramesh serve`). **Nothing can start a durable run yet:**
   there is no starter command or API endpoint (only the library's `start_run`), and the worker
   does not define budgets, so a run needs its budgets defined in the store first. Staging
   must still supply the team folder and a Temporal server (the dev server is for evaluation only).
3. **No `--version` flag** in the CLI; the smoke test uses `--help`, `import nuramesh` and `id -u`.
4. The anchor image doubles as the staging `job_image`; the cost-check job has no code yet.
5. Image names: `infra/staging/variables.tf` already has `api_image`, `worker_image`, `job_image`
   (by digest, placeholder defaults); unchanged. Push and digest recording are a later, owner-approved step.
