"""Start one command under `bwrap` and wait for it (R56, "Descriptors and kill").

`bwrap` gets exactly these descriptors: 0 = `/dev/null`, 1 and 2 = two pipes, the seccomp filter
and the status pipe; everything else is closed. The child raises its own OOM score to 1000 before
it becomes `bwrap` (a small trampoline, so no Python code runs between fork and exec in a process
with threads). On the time limit, a client close or a shutdown the namespace's first process is
killed (which ends every process in it), then `bwrap`; both are reaped. An exit code is reported
only for a command that started and ended by itself: a kill is never a pass.
"""

import ctypes
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from nuramesh.core.sandbox import bwrap_args

# Runs in the child before `bwrap`: R56 "Surviving a command at the limit".
_TRAMPOLINE = (
    "import os,sys\n"
    "fd=os.open('/proc/self/oom_score_adj',os.O_WRONLY)\n"
    "os.write(fd,b'1000')\n"
    "os.close(fd)\n"
    "os.execv(sys.argv[1],sys.argv[1:])\n"
)
KILL_WAIT_SECONDS = 5.0


@dataclass(frozen=True)
class Outcome:
    """What happened to one command. `exit_code` is None unless it ended by itself."""

    started: bool
    exit_code: int | None
    timed_out: bool
    cancelled: bool  # the client closed, sent bytes it must not send, or the service is stopping
    reaped: bool  # False: bwrap did not end within 5 s of the kill
    duration_ms: int
    stdout: bytes
    stderr: bytes
    truncated: bool


PR_SET_CHILD_SUBREAPER = 36


def adopt_orphans() -> bool:
    """Make this process the one that adopts (and so can reap) every orphan below it.

    In its container the supervisor is process 1 and adopts them anyway. Anywhere else (a host,
    a container started with an init process) a killed command whose `bwrap` died first would stay
    a zombie under a process that may reap late or never. False when the kernel refuses.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        return bool(libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0)
    except (OSError, AttributeError):
        return False


def _kill(pid: int | None) -> None:
    if pid:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _client_gone(client: socket.socket) -> bool:
    """True when the client closed, reset, or sent a byte after its request."""
    try:
        client.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
    except BlockingIOError:
        return False
    except OSError:
        return True
    return True  # end of stream, or trailing bytes: either way this request is over


def run_command(
    *,
    bwrap: Sequence[str],
    rootfs: str,
    copy: str,
    filter_bytes: bytes,
    command: Sequence[str],
    env: Mapping[str, str],
    timeout: float,
    output_bytes: int,
    tmp_bytes: int,
    client: socket.socket | None = None,
    stopping: Callable[[], bool] = lambda: False,
) -> Outcome:
    """Run `command` in the sandbox; return when it and `bwrap` are gone (or 5 s after a kill)."""
    filter_r, filter_w = os.pipe()
    status_r, status_w = os.pipe()
    os.set_blocking(filter_w, False)
    try:
        if os.write(filter_w, filter_bytes) != len(filter_bytes):
            raise OSError("the seccomp filter does not fit a pipe")
    finally:
        os.close(filter_w)
    args = bwrap_args(
        rootfs=rootfs,
        workspace_copy=copy,
        env=env,
        tmp_bytes=tmp_bytes,
        seccomp_fd=filter_r,
        status_fd=status_w,
        command=command,
    )
    started_at = time.monotonic()
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _TRAMPOLINE, *bwrap, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(filter_r, status_w),
            env={},
            cwd="/",
        )
    finally:
        os.close(filter_r)
        os.close(status_w)
    assert process.stdout is not None and process.stderr is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "out")
    selector.register(process.stderr, selectors.EVENT_READ, "err")
    selector.register(status_r, selectors.EVENT_READ, "status")
    if client is not None:
        selector.register(client, selectors.EVENT_READ, "client")
    tails = {"out": b"", "err": b""}
    status = b""
    child_pid: int | None = None
    open_pipes = 3
    timed_out = cancelled = truncated = False
    killed_at: float | None = None
    while open_pipes or process.poll() is None:
        now = time.monotonic()
        if killed_at is None:
            if now - started_at >= timeout:
                timed_out = True
            elif stopping():
                cancelled = True
            if timed_out or cancelled:
                killed_at = now
                _kill(child_pid)
                _kill(process.pid)
        elif now - killed_at > KILL_WAIT_SECONDS:
            break
        wait = 0.2 if killed_at is not None else min(0.2, max(0.0, timeout - (now - started_at)))
        for key, _ in selector.select(wait):
            if key.data == "client":
                assert client is not None
                if killed_at is None and _client_gone(client):
                    cancelled = True
                    killed_at = time.monotonic()
                    _kill(child_pid)
                    _kill(process.pid)
                if killed_at is not None:
                    selector.unregister(client)
                continue
            fd = key.fd
            data = os.read(fd, 65536)
            if not data:
                selector.unregister(key.fileobj)
                open_pipes -= 1
            elif key.data == "status":
                status = (status + data)[-65536:]
                for line in status.split(b"\n"):
                    try:
                        found = json.loads(line).get("child-pid")
                    except (ValueError, AttributeError):
                        continue
                    if isinstance(found, int) and found > 1 and child_pid is None:
                        child_pid = found
                        if killed_at is not None:
                            _kill(child_pid)
            else:
                joined = tails[key.data] + data
                if len(joined) > output_bytes:
                    truncated = True
                    joined = joined[-output_bytes:]
                tails[key.data] = joined
        if not open_pipes and client is None:
            time.sleep(0.02)  # nothing left to select on: wait for bwrap or the time limit
    selector.close()
    reaped = True
    try:
        code = process.wait(timeout=0.01)
    except subprocess.TimeoutExpired:
        _kill(child_pid)
        _kill(process.pid)
        try:
            code = process.wait(timeout=KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            code, reaped = -signal.SIGKILL, False
    process.stdout.close()
    process.stderr.close()
    os.close(status_r)
    ended_alone = child_pid is not None and killed_at is None and code >= 0
    return Outcome(
        started=child_pid is not None,
        exit_code=code if ended_alone else None,
        timed_out=timed_out,
        cancelled=cancelled,
        reaped=reaped,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        stdout=tails["out"],
        stderr=tails["err"],
        truncated=truncated,
    )
