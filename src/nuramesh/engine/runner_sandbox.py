"""Which sandbox a runner uses (real-models spec R40 to R42).

The runner (`nuramesh worker --engine light`) chooses once at its start, from its own environment:

1. `NURAMESH_SANDBOX_SOCKET` set and not empty: the **service** sandbox (`RemoteSandbox`). The
   value must be an absolute path, else the runner exits 2. No host runtime is even looked for.
2. Otherwise a working container runtime on the host, Podman first, then Docker: the **host**
   sandbox (`OciSandbox`, exactly as the CLI).
3. Otherwise **none**: opening it raises `SandboxUnavailable`, so a command gate is `error`.

The unsandboxed opt-out (`HostSandbox`) does not exist here: there is no flag and no variable.
The pure rule is `nuramesh.core.sandbox_service.choose_runner_sandbox` (cases G01 to G11).
"""

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Final

from nuramesh.core.sandbox import Sandbox, SandboxUnavailable
from nuramesh.core.sandbox_service import (
    HOST,
    HOST_RUNTIMES,
    PROBE_MAX_AGE_SECONDS,
    SERVICE,
    choose_runner_sandbox,
    runner_sandbox_ready,
)
from nuramesh.engine.sandbox import OciSandbox, detect_runtime
from nuramesh.engine.sandbox_remote import RemoteSandbox

SOCKET_ENV: Final = "NURAMESH_SANDBOX_SOCKET"
UID_ENV: Final = "NURAMESH_SANDBOX_UID"

Detect = Callable[[tuple[str, ...]], str | None]
"""`(candidate names) -> the path of the first that works`, as `engine.sandbox.detect_runtime`."""


class RunnerSandboxError(ValueError):
    """The runner must not start (exit 2). A fixed text: no value of the environment."""


def _uid(text: str | None) -> int | None:
    """The sandbox's user id for the peer check (R59); None when it is missing or not a plain
    number, and then no peer ever passes (fail closed)."""
    if text is None or not text.isascii() or not text.isdigit():
        return None
    return int(text)


@dataclass
class RunnerSandbox:
    """The choice of one runner process. `kind` is `service`, `host` or `none` (the value of the
    `runner.real_models_on` entry, R6)."""

    kind: str
    runtime: str | None = None
    """The runtime's name for `host` (`podman` or `docker`)."""
    socket_path: str | None = None
    peer_uid: int | None = None
    runtime_path: str | None = None
    _probe: RemoteSandbox | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.kind == SERVICE and self.socket_path is not None:
            self._probe = RemoteSandbox(self.socket_path, peer_uid=self.peer_uid)

    def ready(self) -> bool:
        """R41. Service: the last health probe answered `ready` (probed again when the answer is
        older than 30 s); host: a runtime was found at start; none: never."""
        if self._probe is not None:
            return self._probe.ready()
        return runner_sandbox_ready(self.kind, runtime_found=self.runtime_path is not None)

    def probe_every(self, stop: threading.Event, seconds: float = PROBE_MAX_AGE_SECONDS) -> None:
        """R52: the runner probes at start and every 30 s. Returns at once; a daemon thread
        probes until `stop` is set. Nothing to do for `host` and `none`."""
        probe = self._probe
        if probe is None:
            return

        def loop() -> None:
            probe.health()
            while not stop.wait(seconds):
                probe.health()

        threading.Thread(target=loop, name="sandbox-health-probe", daemon=True).start()

    def open(self, stop: Callable[[], bool] | None = None) -> Sandbox:
        """The sandbox for one run. `stop` (service only) is asked every 2 s while a command
        runs: a stop request or a recorded halt closes the connection (R27)."""
        if self.kind == SERVICE and self.socket_path is not None:
            return RemoteSandbox(self.socket_path, peer_uid=self.peer_uid, stop=stop)
        if self.kind == HOST and self.runtime_path is not None:
            return OciSandbox(self.runtime_path, sweep=False)
        raise SandboxUnavailable(
            "the runner has no sandbox: set NURAMESH_SANDBOX_SOCKET to the sandbox service's "
            "socket, or install Podman or Docker on the runner's machine"
        )


def _detect(candidates: tuple[str, ...]) -> str | None:
    return detect_runtime(candidates)


def choose(environ: Mapping[str, str], *, detect: Detect = _detect) -> RunnerSandbox:
    """R40: the choice of this runner. Raises `RunnerSandboxError` when it must exit 2."""
    value = environ.get(SOCKET_ENV)
    found: dict[str, str] = {}
    if not value:  # a set socket never falls back to the host, so the host is not looked at
        for name in HOST_RUNTIMES:
            path = detect((name,))
            if path is not None:
                found[name] = path
                break  # Podman first: Docker is not asked when Podman works
    choice = choose_runner_sandbox(value, list(found))
    if choice.exit_code is not None or choice.kind is None:
        raise RunnerSandboxError(f"{SOCKET_ENV} must be an absolute path")
    if choice.kind == SERVICE:
        return RunnerSandbox(SERVICE, socket_path=value, peer_uid=_uid(environ.get(UID_ENV)))
    if choice.kind == HOST and choice.runtime is not None:
        path = found[choice.runtime]
        OciSandbox(path)  # as the CLI: one sweep of left-over containers and copies at start
        return RunnerSandbox(HOST, runtime=choice.runtime, runtime_path=path)
    return RunnerSandbox(choice.kind)
