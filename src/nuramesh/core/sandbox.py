"""The sandbox: run a command in an isolated copy of a workspace, under limits (R1.08).

This module is the **interface** and the rules; running containers is `engine.sandbox`.

- A `SandboxRequest` names an OCI image **pinned by digest** (`name@sha256:<64 hex>`: a tag can
  move, a digest can't), the command as an argument list (never a shell string), a small
  environment and the limits (memory, CPUs, processes, wall-clock time, output kept).
- A `Sandbox` runs it against a **copy** of the workspace (the original is never changed) and
  returns a `SandboxResult`: exit code, whether the time limit hit, the tail of the output, the
  duration and which isolation was used.
- `oci_run_args` builds the hardened `run` command line for Podman or Docker: no network,
  read-only root filesystem with a small `/tmp`, the workspace copy as the only writable mount
  (`--mount`, so a missing source is an error), all
  capabilities dropped, `no-new-privileges`, a non-root user, memory/CPU/process limits, the
  default seccomp profile (never `unconfined`).
- `TestCommandGateConfig` is a test-command gate's configuration.

A timeout or a sandbox that can't start is an **error**, never a pass (T5). Environment variables
named `NURAMESH_*` (the product's own settings and secrets) can't be passed into a sandbox.
"""

import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

IMAGE_PATTERN = r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$"
_ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
SANDBOX_USER = "65532:65532"  # "nonroot" in distroless images; any non-zero id would do
WORKDIR = "/workspace"
SERVICE_RUNTIME = "sandboxd/bwrap"  # `SandboxResult.runtime` of the sandbox service (R58)
SERVICE_COMMAND_ID = 65532  # uid and gid of a command in the sandbox service (R56)

Image = Annotated[str, StringConstraints(pattern=IMAGE_PATTERN, max_length=300)]
Arg = Annotated[str, StringConstraints(min_length=1, max_length=4_096)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SandboxLimits(_Frozen):
    """Resource limits for one sandboxed command."""

    memory_mb: int = Field(default=1_024, ge=64, le=16_384)
    cpus: Decimal = Field(default=Decimal("1"), ge=Decimal("0.1"), le=Decimal("16"))
    pids: int = Field(default=256, ge=16, le=4_096)
    timeout_seconds: int = Field(default=300, ge=1, le=3_600)
    output_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)


def _check_args(args: Sequence[str]) -> None:
    if any("\x00" in a for a in args):
        raise ValueError("command arguments can't contain NUL")


def _check_env(env: Mapping[str, str]) -> None:
    for key, value in env.items():
        if not _ENV_KEY.match(key):
            raise ValueError(f"environment variable name '{key[:40]}' isn't allowed")
        if key.startswith("NURAMESH_"):
            raise ValueError("NURAMESH_* settings are never passed into a sandbox")
        if "\x00" in value or len(value) > 4_096:
            raise ValueError(f"environment variable '{key}' has an invalid value")


class SandboxRequest(_Frozen):
    """What to run: a digest-pinned image, an argument list, an environment and limits."""

    image: Image
    command: tuple[Arg, ...] = Field(min_length=1, max_length=100)
    env: Mapping[str, str] = Field(default_factory=dict, max_length=50)
    limits: SandboxLimits = SandboxLimits()

    @field_validator("command")
    @classmethod
    def _no_nul(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _check_args(value)
        return value

    @field_validator("env")
    @classmethod
    def _safe_env(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        _check_env(value)
        return value


class Isolation(StrEnum):
    """How a command was isolated."""

    OCI = "oci"
    HOST = "host"  # the LOCAL-only opt-out: no isolation, recorded in the ledger
    SERVICE = "service"  # the team server's sandbox service (`sandboxd`, real-models spec R58)


class SandboxResult(_Frozen):
    """What happened: exit code (None if the command never ran or was killed), output tails."""

    exit_code: int | None
    timed_out: bool
    duration_ms: int = Field(ge=0)
    stdout_tail: str = ""
    stderr_tail: str = ""
    isolation: Isolation
    runtime: str
    started: bool = True  # False when the sandbox itself couldn't start the command
    # Set by the sandbox service only (real-models spec R58): a tail was cut, and the SHA-256 of
    # the workspace archive as the service counted it.
    output_truncated: bool = False
    workspace_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    # True only when the engine's own stop cut the command (R27); never read from output.
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        """The command ran to completion and exited 0."""
        return self.started and not self.timed_out and self.exit_code == 0


class Sandbox(Protocol):
    """Runs a request against a copy of `workspace` (a local directory path)."""

    def run(self, request: SandboxRequest, workspace: str) -> SandboxResult: ...


class SandboxUnavailable(RuntimeError):
    """No sandbox can run here (no container runtime, or hosted mode); nothing was run."""


class TestCommandGateConfig(_Frozen):
    """A test-command gate: the command must exit 0 in the sandbox for its criteria to be met."""

    __test__ = False  # not a pytest test class

    image: Image
    command: tuple[Arg, ...] = Field(min_length=1, max_length=100)
    env: Mapping[str, str] = Field(default_factory=dict, max_length=50)
    limits: SandboxLimits = SandboxLimits()

    @field_validator("command")
    @classmethod
    def _no_nul(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _check_args(value)
        return value

    @field_validator("env")
    @classmethod
    def _safe_env(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        _check_env(value)
        return value

    def request(self) -> SandboxRequest:
        """The sandbox request this gate runs."""
        return SandboxRequest(
            image=self.image, command=self.command, env=self.env, limits=self.limits
        )

    @classmethod
    def parse(cls, config: Mapping[str, object]) -> Self:
        """Validate a gate's config mapping."""
        return cls.model_validate(config)


def oci_run_args(
    runtime: str,
    request: SandboxRequest,
    *,
    workspace_copy: str,
    name: str,
    podman: bool,
    user: str = SANDBOX_USER,
    keep_id: bool = True,
) -> list[str]:
    """The hardened `<runtime> run ...` argument list (module docstring). Never a shell string."""
    limits = request.limits
    args = [
        runtime,
        "run",
        "--rm",
        "--name",
        name,
        "--network=none",
        "--read-only",
        "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--user={user}",
        f"--memory={limits.memory_mb}m",
        f"--memory-swap={limits.memory_mb}m",
        f"--cpus={limits.cpus.normalize():f}",
        f"--pids-limit={limits.pids}",
        "--pull=never",
        f"--mount=type=bind,src={workspace_copy},dst={WORKDIR}",  # fails if the source is missing
        f"--workdir={WORKDIR}",
    ]
    if podman:
        if keep_id:  # rootless only: files in the copy stay owned by the caller
            args.insert(2, "--userns=keep-id")
        args.insert(2, "--http-proxy=false")  # the host's proxy settings stay on the host
    else:  # the Docker CLI injects proxy settings from its config; blank them explicitly
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "NO_PROXY"):
            args.insert(2, f"--env={name}=")
            args.insert(2, f"--env={name.lower()}=")
    for key in sorted(request.env):
        args.append(f"--env={key}={request.env[key]}")
    # The program is the entrypoint and the rest are its arguments: the image's own ENTRYPOINT
    # (which could be `sh -c`) never re-parses the command, so what the policy approved is what
    # executes.
    # Podman reads an entrypoint that looks like a JSON array as that array (`["sh","-c","x"]`), so
    # for Podman the program is always a one-element JSON array: it can only be one program.
    program = json.dumps([request.command[0]]) if podman else request.command[0]
    args.append(f"--entrypoint={program}")
    args.append(request.image)
    args.extend(request.command[1:])
    return args


def bwrap_args(
    *,
    rootfs: str,
    workspace_copy: str,
    env: Mapping[str, str],
    tmp_bytes: int,
    seccomp_fd: int,
    status_fd: int,
    command: Sequence[str],
) -> list[str]:
    """The `bwrap` argument list of the sandbox service, exactly R56 (real-models spec).

    One command in new user, PID, network, IPC, UTS and cgroup namespaces, no nested user
    namespace, uid and gid 65532, no capability, a read-only root, a read-only fresh `/proc`, a
    sized `/tmp`, the workspace copy as `/workspace`, the command's own seccomp filter. No `-try`
    form of any option: a host that cannot do one of these fails, it is never skipped. The
    environment is written in sorted order so the list is the same for the same request.
    """
    args = [
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--unshare-cgroup",
        "--disable-userns",
        "--uid",
        str(SERVICE_COMMAND_ID),
        "--gid",
        str(SERVICE_COMMAND_ID),
        "--die-with-parent",
        "--new-session",
        "--clearenv",
    ]
    for key in sorted(env):
        args += ["--setenv", key, env[key]]
    args += [
        "--cap-drop",
        "ALL",
        "--ro-bind",
        rootfs,
        "/",
        "--proc",
        "/proc",
        "--remount-ro",
        "/proc",
        "--dev",
        "/dev",
        "--size",
        str(tmp_bytes),
        "--tmpfs",
        "/tmp",
        "--bind",
        workspace_copy,
        WORKDIR,
        "--chdir",
        WORKDIR,
        "--seccomp",
        str(seccomp_fd),
        "--json-status-fd",
        str(status_fd),
        "--",
    ]
    return [*args, *command]
