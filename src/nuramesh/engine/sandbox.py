"""Run commands in hardened OCI containers, and the test-command gate (R1.08).

- `detect_runtime` finds a working container runtime: **Podman first, then Docker** (Docker Engine
  on Linux, or the `docker` CLI of Colima on a Mac). Docker Desktop works but isn't required or
  recommended. A runtime counts only if `<runtime> info` answers.
- `OciSandbox` copies the workspace into a fresh directory under `~/.nuramesh/sandbox/` (the home
  folder is shared with the Podman and Colima VMs by default on a Mac), runs the request in a
  container with the hardened flags of `core.sandbox.oci_run_args` (bind-mounted with `--mount`,
  which fails on a missing source instead of mounting an empty folder), enforces the wall-clock
  limit itself, keeps only the tail of the output **while reading it** (the engine never holds more
  than the limit in memory), and always deletes the copy. The original workspace is never mounted.
  Every failure of the runtime, the copy or the clean-up becomes a result with `started=False` or
  `timed_out=True`, never an exception that escapes a gate.
- **Containers never outlive the engine on purpose:** each one carries a label with the engine's
  process id; on timeout, error or interrupt (including Ctrl-C) the engine kills and removes it,
  and a new `OciSandbox` removes labelled containers whose engine process is gone. Podman also gets
  `--timeout` as a backstop.
- `open_sandbox` is the only supported way to get a sandbox, and the caller must say where it runs
  (`mode`): **hosted mode refuses** (hosted command execution stays closed until a stronger tier is
  chosen); with no runtime it **refuses** (`SandboxUnavailable`) unless the LOCAL-only opt-out
  is set, in which case it prints a loud warning **every time**, records `sandbox.opt_out` in the
  run's ledger and returns a `HostSandbox` (no isolation: same copy, time and output limits only;
  processes that leave their session can outlive it).
- `command_gate_runner` turns a sandbox into a runner for `test_command` gates: exit 0 meets every
  criterion the gate checks; another exit code fails them; a timeout, a sandbox that couldn't start
  the command, or a command missing from the image (exit 126/127) is an `error`, never a pass.

Time spent pulling a missing image (bounded separately) and copying the workspace is outside the
command's limit. The copy and the writable mount have no size limit yet: keep gate workspaces small.

Install a runtime (no licence fee): `brew install podman && podman machine init && podman machine
start`, or `brew install colima docker && colima start`. On Linux, Podman or Docker Engine.
"""

import collections
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import warnings
from collections.abc import Callable
from enum import StrEnum
from io import BufferedReader
from typing import IO, Any, cast

from nuramesh.core.canonical import thaw
from nuramesh.core.gates import GateResult, GateSpec
from nuramesh.core.ledger import EventType
from nuramesh.core.sandbox import (
    SANDBOX_USER,
    Isolation,
    Sandbox,
    SandboxRequest,
    SandboxResult,
    SandboxUnavailable,
    TestCommandGateConfig,
    oci_run_args,
)
from nuramesh.core.tasks import Task
from nuramesh.engine.gate_runners import error_result, judged_result
from nuramesh.paths import home_path
from nuramesh.stores.base import EventDraft, Store, run_stream

RUNTIMES = ("podman", "docker")
LABEL = "nuramesh.sandbox.owner"
STALE_COPY_SECONDS = 24 * 3600
CANCELLED_TEXT = "the run was stopped"


class CommandCancelled(Exception):  # noqa: N818 (named for the spec, like `CallCancelled`)
    """A stop request or a halt cut a command gate's command (the real-models spec R27, C37s): the
    command did not complete, so the gate has no result (not even `error`). The engine decides
    what follows (the stop path) and writes no gate result. `isolation` names the sandbox that
    was cut (None when the raiser did not say): the engine's scope rule reads it (decision note
    x-963-pr3-cut-gate)."""

    def __init__(self, message: str = "", *, isolation: Isolation | None = None) -> None:
        super().__init__(message)
        self.isolation = isolation


CANCEL_POLL = 0.1  # seconds between looks at the cancel flag while a command runs
CLI_TIMEOUT = 30  # seconds for each runtime housekeeping call (inspect, kill, rm, ps)
PULL_TIMEOUT = 600
_START_FAILED = {125}  # the runtime couldn't create or start the container
_NOT_RUNNABLE = {126, 127}  # the command isn't in the image or isn't executable


class Mode(StrEnum):
    """Where NuraMesh runs: on the operator's machine, or as the hosted service."""

    LOCAL = "local"
    HOSTED = "hosted"


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill a process and everything it started in its session."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _cli(
    args: list[str], timeout: float | None = None
) -> subprocess.CompletedProcess[bytes] | None:
    """Run a housekeeping command; None if it couldn't run or took too long.

    Runs in its own session so a timeout kills the whole process group: a child that holds the
    output pipes can't keep us waiting past the deadline.
    """
    limit = CLI_TIMEOUT if timeout is None else timeout
    try:
        proc = subprocess.Popen(  # noqa: S603 (an argument list, no shell)
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return None
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return None
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def detect_runtime(candidates: tuple[str, ...] = RUNTIMES, timeout: float = 15) -> str | None:
    """The path of the first container runtime that answers `info`, or None."""
    for name in candidates:
        path = shutil.which(name)
        if path is None:
            continue
        done = _cli([path, "info"], timeout)
        if done is not None and done.returncode == 0:
            return path
    return None


def _sandbox_root() -> str:
    root = str(home_path("sandbox"))
    os.makedirs(root, mode=0o700, exist_ok=True)
    return root


def _copy_workspace(workspace: str, root: str | None = None) -> str:
    """Copy `workspace` to a fresh private directory; returns the copy (raises on failure)."""
    if not os.path.isdir(workspace):
        raise SandboxUnavailable("the workspace isn't a directory")
    parent = tempfile.mkdtemp(prefix="ws-", dir=root)
    target = os.path.join(parent, "ws")
    try:
        # Links are copied as links (never followed); sockets, FIFOs and unreadable files fail.
        shutil.copytree(workspace, target, symlinks=True)
    except BaseException:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    return target


class _Tail(threading.Thread):
    """Reads a pipe to the end, keeping only the last `limit` bytes in memory."""

    def __init__(self, pipe: IO[bytes], limit: int) -> None:
        super().__init__(daemon=True)
        self.pipe, self.limit = pipe, limit
        self.chunks: collections.deque[bytes] = collections.deque()
        self.size = 0

    def run(self) -> None:
        try:
            while chunk := cast(BufferedReader, self.pipe).read1(65_536):
                self.chunks.append(chunk)
                self.size += len(chunk)
                while self.size - len(self.chunks[0]) >= self.limit:
                    self.size -= len(self.chunks.popleft())
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        data = b"".join(self.chunks)[-self.limit :]
        return data.decode("utf-8", "replace")


def _run_bounded(
    proc: subprocess.Popen[bytes],
    timeout: int,
    limit: int,
    on_timeout: Callable[[], None],
    after_exit: Callable[[], None] | None = None,
    cancelled: threading.Event | None = None,
) -> tuple[int | None, bool, str, str]:
    """Wait for `proc` with a deadline, reading output into bounded tails.

    If `cancelled` is set while waiting (the run was stopped) the command is killed the same way a
    timeout kills it, and the result says `timed_out` (callers check `cancelled` to tell which)."""
    assert proc.stdout is not None and proc.stderr is not None
    readers = [_Tail(proc.stdout, limit), _Tail(proc.stderr, limit)]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        deadline = time.monotonic() + timeout
        while True:
            if cancelled is not None and cancelled.is_set():
                raise subprocess.TimeoutExpired(proc.args, timeout)
            left = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=left if cancelled is None else min(CANCEL_POLL, left))
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
        if after_exit is not None:
            after_exit()  # background processes of the command don't outlive it
    except subprocess.TimeoutExpired:
        timed_out = True
        on_timeout()
        try:
            proc.wait(timeout=CLI_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
    for reader in readers:
        reader.join(timeout=5)  # a pipe held open by an escaped process is abandoned, not awaited
    code = None if timed_out else proc.returncode
    return code, timed_out, readers[0].text(), readers[1].text()


def removed_copies(root: str) -> int:
    """Delete workspace copies older than a day (an engine killed before its clean-up)."""
    count = 0
    cutoff = time.time() - STALE_COPY_SECONDS
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for entry in entries:
        path = os.path.join(root, entry)
        try:
            if entry.startswith("ws-") and os.stat(path).st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                count += 1
        except OSError:
            continue
    return count


def _owner_tag() -> str:
    """`<host>/<pid>`: who started a container (a pid alone means nothing across hosts)."""
    return f"{socket.gethostname()}/{os.getpid()}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class OciSandbox:
    """A `Sandbox` backed by a Podman or Docker runtime (module docstring)."""

    def __init__(self, runtime: str, *, sweep: bool = True) -> None:
        self.runtime = runtime
        self._cancelled = threading.Event()
        version = _cli([runtime, "--version"])
        text = version.stdout.decode("utf-8", "replace").lower() if version else ""
        self.podman = "podman" in text or os.path.basename(runtime).startswith("podman")
        if sweep:
            self.remove_orphans()

    def cancel(self) -> None:
        """The run was stopped: kill and remove the container now running, and refuse new ones."""
        self._cancelled.set()

    def remove_orphans(self) -> int:
        """Remove labelled containers whose engine process is gone; returns how many."""
        removed = 0
        listed = _cli(
            [
                self.runtime,
                "ps",
                "-a",
                "--filter",
                f"label={LABEL}",
                "--format",
                f'{{{{.Names}}}} {{{{.Label "{LABEL}"}}}}',
            ]
        )
        if listed is None or listed.returncode != 0:
            return 0
        for line in listed.stdout.decode("utf-8", "replace").splitlines():
            name, _, tag = line.strip().partition(" ")
            host, _, owner = tag.rpartition("/")
            if (
                name.startswith("nuramesh-")
                and owner.isdigit()
                and host == socket.gethostname()  # never touch another host's containers
                and not _alive(int(owner))
            ):
                if _cli([self.runtime, "rm", "-f", name]) is not None:
                    removed += 1
        removed_copies(_sandbox_root())
        return removed

    def _ensure_image(self, image: str) -> bool:
        inspect = _cli([self.runtime, "image", "inspect", image])
        if inspect is not None and inspect.returncode == 0:
            return True
        pull = _cli([self.runtime, "pull", image], PULL_TIMEOUT)
        return pull is not None and pull.returncode == 0

    def _stop(self, name: str) -> None:
        _cli([self.runtime, "rm", "-f", name])  # -f kills a running container first

    def run(self, request: SandboxRequest, workspace: str) -> SandboxResult:
        """Run the request in a container against a copy of `workspace` (never raises)."""
        limits = request.limits
        started = time.monotonic()

        def result(**kw: Any) -> SandboxResult:
            return SandboxResult(
                duration_ms=int((time.monotonic() - started) * 1000),
                isolation=Isolation.OCI,
                runtime=os.path.basename(self.runtime),
                **kw,
            )

        def not_started(why: str, cancelled: bool = False) -> SandboxResult:
            return result(
                exit_code=None,
                timed_out=False,
                started=False,
                stderr_tail=why,
                cancelled=cancelled,
            )

        if self._cancelled.is_set():
            return not_started(CANCELLED_TEXT, cancelled=True)
        try:
            copy = _copy_workspace(workspace, _sandbox_root())
        except SandboxUnavailable as exc:
            return not_started(str(exc))
        except (OSError, shutil.Error) as exc:
            return not_started(f"the workspace couldn't be copied ({type(exc).__name__})")
        name = f"nuramesh-{uuid.uuid4().hex[:16]}"
        try:
            if any(c in copy for c in ",:\n"):
                return not_started("the sandbox folder's path can't be mounted (',' or ':' in it)")
            if not self._ensure_image(request.image):
                return not_started("the image couldn't be found or pulled")
            uid = os.getuid() if hasattr(os, "getuid") else 0
            if uid == 0:  # a root caller: open the copy to the unprivileged user, keep exec bits
                for path, _dirs, files in os.walk(copy):
                    for entry in (path, *(os.path.join(path, f) for f in files)):
                        if not os.path.islink(entry):
                            mode = os.stat(entry).st_mode
                            os.chmod(entry, mode | (0o777 if os.path.isdir(entry) else 0o666))
            user = f"{uid}:{os.getgid()}" if uid != 0 else SANDBOX_USER
            args = oci_run_args(
                self.runtime,
                request,
                workspace_copy=copy,
                name=name,
                podman=self.podman,
                user=user,
                keep_id=uid != 0,
            )
            args.insert(2, f"--label={LABEL}={_owner_tag()}")
            if self.podman:
                args.insert(2, f"--timeout={limits.timeout_seconds + 30}")  # a backstop only
            proc = subprocess.Popen(  # noqa: S603 (an argument list, no shell)
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

            def on_timeout() -> None:
                self._stop(name)
                _kill_group(proc)

            try:
                code, timed_out, out, err = _run_bounded(
                    proc,
                    limits.timeout_seconds,
                    limits.output_bytes,
                    on_timeout,
                    cancelled=self._cancelled,
                )
            except BaseException:  # an interrupt (Ctrl-C) must not leave the container running
                _kill_group(proc)
                self._stop(name)
                raise
            if timed_out and self._cancelled.is_set():  # it did start; the stop killed it
                return result(
                    exit_code=None,
                    timed_out=False,
                    started=True,
                    stdout_tail=out,
                    stderr_tail=(err + "\n" + CANCELLED_TEXT).strip(),
                    cancelled=True,
                )
            if timed_out:
                return result(exit_code=None, timed_out=True, stdout_tail=out, stderr_tail=err)
            return result(
                exit_code=code,
                timed_out=False,
                started=code not in _START_FAILED,
                stdout_tail=out,
                stderr_tail=err,
            )
        except OSError as exc:
            return not_started(f"the container runtime couldn't be run ({type(exc).__name__})")
        finally:
            shutil.rmtree(os.path.dirname(copy), ignore_errors=True)


class HostSandbox:
    """The LOCAL-only opt-out: runs on the host, in a copy, with time and output limits only.

    **No isolation**: the command can read the host, use the network and run as the caller, and a
    process that leaves its session isn't stopped. Only `open_sandbox` should create it, after the
    warning and the `sandbox.opt_out` ledger event.
    """

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        """The run was stopped: kill the command now running and refuse new ones."""
        self._cancelled.set()

    def run(self, request: SandboxRequest, workspace: str) -> SandboxResult:
        """Run the command on the host against a copy of `workspace` (never raises)."""
        limits = request.limits
        started = time.monotonic()

        def result(**kw: Any) -> SandboxResult:
            return SandboxResult(
                duration_ms=int((time.monotonic() - started) * 1000),
                isolation=Isolation.HOST,
                runtime="host",
                **kw,
            )

        if self._cancelled.is_set():
            return result(
                exit_code=None,
                timed_out=False,
                started=False,
                stderr_tail=CANCELLED_TEXT,
                cancelled=True,
            )
        try:
            copy = _copy_workspace(workspace)
        except SandboxUnavailable as exc:
            return result(exit_code=None, timed_out=False, started=False, stderr_tail=str(exc))
        except (OSError, shutil.Error) as exc:
            why = f"the workspace couldn't be copied ({type(exc).__name__})"
            return result(exit_code=None, timed_out=False, started=False, stderr_tail=why)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": copy, **request.env}
        try:
            proc = subprocess.Popen(  # noqa: S603 (an argument list, no shell)
                list(request.command),
                cwd=copy,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

            try:
                code, timed_out, out, err = _run_bounded(
                    proc,
                    limits.timeout_seconds,
                    limits.output_bytes,
                    lambda: _kill_group(proc),
                    after_exit=lambda: _kill_group(proc),
                    cancelled=self._cancelled,
                )
            except BaseException:
                _kill_group(proc)
                raise
            if timed_out and self._cancelled.is_set():  # it did start; the stop killed it
                return result(
                    exit_code=None,
                    timed_out=False,
                    started=True,
                    stdout_tail=out,
                    stderr_tail=(err + "\n" + CANCELLED_TEXT).strip(),
                    cancelled=True,
                )
            return result(exit_code=code, timed_out=timed_out, stdout_tail=out, stderr_tail=err)
        except OSError as exc:
            why = f"the command couldn't be started ({type(exc).__name__})"
            return result(exit_code=None, timed_out=False, started=False, stderr_tail=why)
        finally:
            shutil.rmtree(os.path.dirname(copy), ignore_errors=True)


def open_sandbox(
    store: Store,
    *,
    run_id: str,
    mode: Mode,
    allow_unsandboxed: bool = False,
    actor: str = "system",
    runtime: str | None = None,
    detect: Callable[[], str | None] = detect_runtime,
) -> Sandbox:
    """The sandbox for a run (module docstring). Raises `SandboxUnavailable` when none may run."""
    if mode is Mode.HOSTED:
        if allow_unsandboxed:
            raise SandboxUnavailable("the unsandboxed opt-out is refused in hosted mode")
        raise SandboxUnavailable(
            "command execution is closed in hosted mode until a stronger sandbox tier is chosen"
        )
    found = runtime or detect()
    if found is not None:
        return OciSandbox(found)
    if not allow_unsandboxed:
        raise SandboxUnavailable(
            "no container runtime found (no Podman or Docker answered `info`): install Podman "
            "(`brew install podman && podman machine init && podman machine start`) or Colima "
            "(`brew install colima docker && colima start`). For a local experiment only, add "
            "--allow-unsandboxed: the commands then run on this machine without isolation, "
            "printed and recorded in the ledger (never a default; refused in hosted mode)"
        )
    message = (
        "SANDBOX OFF: commands will run on this machine WITHOUT isolation (local opt-out). "
        "This is recorded in the ledger."
    )
    print(f"\n*** {message} ***\n", file=sys.stderr)  # every time, not once per process
    warnings.warn(message, stacklevel=2)
    with store.transaction() as uow:
        uow.append(
            EventDraft(
                stream=run_stream(run_id),
                type=EventType.SANDBOX_OPT_OUT,
                actor=actor,
                run_id=run_id,
                payload={
                    "mode": mode.value,
                    "isolation": Isolation.HOST.value,
                    "warning": "commands run on the host without isolation",
                },
            )
        )
    return HostSandbox()


def _service_evidence(image: str, outcome: SandboxResult) -> str:
    """What R58 adds to the gate evidence of a sandbox service result (real-models spec).

    Fixed text only: the digest of the image used, the SHA-256 of the workspace archive as the
    service counted it, and whether the service cut the output. Empty for `oci` and `host`.
    """
    if outcome.isolation is not Isolation.SERVICE:
        return ""
    digest = image.rsplit("@sha256:", 1)[-1]
    archive = outcome.workspace_sha256 or "not reported"
    truncated = "yes" if outcome.output_truncated else "no"
    return f"; image sha256:{digest}; archive sha256 {archive}; output truncated: {truncated}"


def command_gate_runner(
    sandbox: Sandbox, workspace: Callable[[Task, Any], str]
) -> Callable[[GateSpec, Task, Any], GateResult]:
    """A gate runner for `test_command` gates; `workspace(task, output)` gives the directory."""

    def run(spec: GateSpec, task: Task, output: Any) -> GateResult:
        checked_by = "gate:test_command"

        def error(message: str) -> GateResult:
            return error_result(
                spec,
                checked_by=checked_by,
                message=message[:2_000],
                task_id=task.id,
                attempt=task.attempt,
                submission=task.submission,
            )

        def judged(met: bool, detail: str) -> GateResult:
            return judged_result(
                spec,
                checked_by=checked_by,
                findings={c: (met, detail) for c in spec.checks},
                task_id=task.id,
                attempt=task.attempt,
                submission=task.submission,
            )

        try:
            config = TestCommandGateConfig.model_validate(thaw(spec.config))
        except ValueError:
            return error("the gate's configuration is invalid")
        try:
            outcome = sandbox.run(config.request(), workspace(task, output))
        except SandboxUnavailable as exc:
            return error(str(exc))
        except Exception as exc:  # a sandbox must never turn a crash into a pass or an escape
            return error(f"the sandbox failed ({type(exc).__name__})")
        if (
            outcome.exit_code is None and not outcome.timed_out and outcome.cancelled
        ):  # the sandbox was cut by a stop or a halt (R27): the engine asks which and stops
            raise CommandCancelled(CANCELLED_TEXT, isolation=outcome.isolation)
        seconds = outcome.duration_ms / 1000
        where = f"{outcome.isolation.value} sandbox ({outcome.runtime})"
        held = _service_evidence(config.image, outcome)  # R58: fixed text, never output
        program = config.command[0]
        if outcome.timed_out:
            return error(f"timed out after {config.limits.timeout_seconds}s in the {where}{held}")
        if not outcome.started or outcome.exit_code is None:
            why = outcome.stderr_tail.strip()[-300:] or "no detail"
            return error(f"the {where} couldn't run the command: {why}{held}")
        if outcome.exit_code in _NOT_RUNNABLE:
            return error(
                f"`{program}` isn't in the image or isn't executable "
                f"(exit {outcome.exit_code}) in the {where}{held}"
            )
        if outcome.exit_code == 0:
            return judged(True, f"`{program}` exited 0 in {seconds:.1f}s in the {where}{held}")
        tail = (outcome.stdout_tail + outcome.stderr_tail).strip()[-400:]
        detail = f"`{program}` exited {outcome.exit_code} in {seconds:.1f}s in the {where}"
        # the retry feedback keeps the start of this text: the output tail goes before the R58 text
        return judged(False, f"{detail}; last output: {tail}{held}" if tail else f"{detail}{held}")

    return run
