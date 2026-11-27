# Sandbox: running commands safely

NuraMesh runs test-command gates (and later any command an agent runs) in a **sandbox**: a hardened
container with no network, a read-only system, no extra privileges, a non-root user, memory, CPU and
process limits, and a time limit NuraMesh enforces itself. The command gets a **copy** of the
workspace; your files are never changed. A timeout or a sandbox that can't start is an `error`,
never a pass.

## Install a container runtime (free)

On a Mac (Apple Silicon), use **Podman** or **Colima**. Docker Desktop works but is not required and
is not the recommended path (its licence is paid for larger companies).

```bash
# Podman
brew install podman
podman machine init
podman machine start
podman info            # should print details, not an error

# or Colima (gives you a `docker` command backed by a small VM)
brew install colima docker
colima start
docker info
```

On Linux, install Podman or Docker Engine from your distribution.

NuraMesh looks for `podman` first, then `docker`, and uses the first one whose `info` works.

## No runtime, no run

Without a runtime, a test-command gate **refuses to run**; it never falls back to your machine.
For local experiments only, there is an opt-out (`allow_unsandboxed=True` in `open_sandbox`; on the command line `--allow-unsandboxed` on `try` and `demo`). It prints a loud warning, records `sandbox.opt_out` in the run's ledger and
runs the command on your machine in a copy of the workspace, with only time and output limits.
**Hosted mode refuses both**: command execution stays closed there until a stronger sandbox tier is
chosen.

## Images

Gate images must be **pinned by digest** (`docker.io/library/python@sha256:…`), because a tag like
`python:3.12` can change under you. Find a digest with
`podman image inspect --format '{{index .RepoDigests 0}}' python:3.12-slim` after pulling it.
NuraMesh pulls a missing image before the run (outside the sandbox); the command itself has no
network.

## Self-hosted Compose stack: the `sandbox` and `runner` services

Status: Coming soon. The images are not published yet, so this stack cannot be started from released images today; this section describes the design.

The Compose stack (its Compose file, see `docs/self-hosting.md`) runs commands in two
extra services instead of a runtime on the host:

- `sandbox` is a small service with no network (`network_mode: none`), a read-only root, all
  capabilities dropped, `no-new-privileges`, a non-root user, and every limit set (memory, swap,
  CPU, processes). With one of those limits missing the service still starts, but it answers every
  command with `sandbox_unlimited` and runs nothing. It copies the workspace into a memory-backed
  `/work` area, and runs each command inside nested namespaces made with `bwrap` (bubblewrap).
- `runner` is the worker that executes a team's tasks with the light engine. It reaches the sandbox
  only through a Unix socket on a volume shared by those two services; it accepts only the sandbox's
  user as the peer and the sandbox answers only the runner's user.

Two settings differ from the container engine's defaults, and each is listed in the Compose file with
its reason: a seccomp profile (a JSON file beside the Compose file) that is the engine's default
profile plus the calls nested namespaces need (`unshare`, namespace `clone`, `mount`, `umount2`,
`pivot_root`), and `systempaths=unconfined` so the command's own PID namespace can mount a fresh
`/proc`. Neither adds a capability.

A command that runs past its time limit gives an `error` gate result ("timed out after ...") and never
counts as a pass. A command that is cut short because the run was stopped or halted has no gate result;
the task is not marked done. This holds for a `runner` started with `--fake` too.

**Supported hosts.** The `sandbox` service runs commands only where the container engine remaps user
IDs, which is the case for **rootless** Docker or Podman on a Linux host with a recent kernel that allows
unprivileged user namespaces inside containers. On a **rootful** engine (rootful Docker on Linux,
Colima, Docker Desktop) the service starts, answers `sandbox_rootful` to every command and runs nothing,
by design. So on a Mac (Colima or Docker Desktop) command gates do not run through this stack today. The
image matrix passed in a rootless container run on the build machine. A full-stack run on a rootless
Linux host has not been verified yet, so treat the sandbox service as not yet supported for production
use.

**What the sandbox does not cover.** Only command gates are sandboxed. Team code (the agents' own
tools and the light engine) runs unsandboxed in the `runner` container. It has the runner's user, so it
can read the stored key files of every organization that runner serves, and it can reach whatever the
`egress` network reaches, including a cloud host's metadata address unless you block it. This is what the
CLI has on a laptop today. An allow-list egress proxy is an enterprise feature.

### Adding an image to the sandbox store

The `sandbox` service reads images from a volume it cannot write. You add one by hand with the
`sandbox-pull` helper, which is not started with the stack:

Build the helper's image first: the first-run build makes only `api worker sandbox`, and the
Compose file never pulls or builds `nuramesh-sandbox-pull` for you (`pull_policy: never`). Run
the same image build script that built `api worker sandbox`, with `sandbox-pull` as its argument, then:

```bash
docker compose --profile sandbox-pull run --rm sandbox-pull NAME@sha256:<64 hex digits>
```

Only a reference with a digest is accepted (a tag alone is refused, and so is a tag next to the digest, `name:tag@sha256:…`; a registry port as in `host:5000/img@sha256:…` is fine). The helper checks that what the
registry returns hashes to the digest you asked for, checks every layer, unpacks without privileges
into a temporary folder, and gives the folder its final name `sha256-<hex>` only when everything is
on disk. It uses no registry sign-in and always verifies TLS. Running it again for an image already
in the store does nothing.
