# Sandbox: running commands safely

NuraMesh runs test-command gates (and later any command an agent runs) in a **sandbox**: a hardened
container with no network, a read-only system, no extra privileges, a non-root user, memory, CPU and
process limits, and a time limit NuraMesh enforces itself. The command gets a **copy** of the
workspace; your files are never changed. A timeout or a sandbox that can't start is an `error`,
never a pass. (Decisions A6, T30.)

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
