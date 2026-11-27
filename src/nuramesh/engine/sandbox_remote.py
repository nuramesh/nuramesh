"""`RemoteSandbox`: the runner's side of the sandbox service (real-models spec R41, R55, R57a, R59).

A `Sandbox` that sends one request per connection to `sandboxd` over its Unix socket (7.5) and
never raises. A call that fails in any way is a result with `started=False` and `exit_code=None`,
so the command gate is `error`, never `passed`:

- the socket path is not a socket, or it or its directory is writable by others;
- no connection within 5 s; the peer is not the sandbox's user (`SO_PEERCRED`, R59);
- no complete answer within `timeout_seconds + 60` s; an answer that is not valid (7.5);
- any refusal code of the service (the code itself is the reason).

The reason is always a fixed text or a fixed code: no path, no output, no text of the service.

The workspace travels as a tar built from the private copy the engine makes (R57a): links are
written as links and never followed, every file is opened with `O_NOFOLLOW` relative to a
directory handle, a socket, FIFO or device fails the call before anything is sent.

A stop (`cancel`, or the `stop` watcher that looks every 2 s, R27 "A command in flight") closes
the connection; the supervisor then kills the command and deletes its copy (R43). That is not a
failed call: the result says the command started and was stopped, as the OCI path does.
"""

import os
import shutil
import socket
import stat
import struct
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable
from typing import IO, Any, Final

from nuramesh.core.sandbox import (
    SERVICE_RUNTIME,
    Isolation,
    SandboxRequest,
    SandboxResult,
    SandboxUnavailable,
)
from nuramesh.core.sandbox_service import NO_ANSWER as NO_ANSWER
from nuramesh.core.sandbox_service import (
    OK,
    PROBE_MAX_AGE_SECONDS,
    PROBE_SECONDS,
    SERVICE,
    ArchiveRefused,
    Op,
    WireError,
    decode_response,
    encode_request,
    response_length,
    runner_sandbox_ready,
)
from nuramesh.engine.sandbox import CANCELLED_TEXT, _copy_workspace, _sandbox_root

NOT_A_SOCKET: Final = (
    "the sandbox socket is not a socket, or it or its directory is writable by others"
)
NO_CONNECTION: Final = "no connection to the sandbox service in time"
WRONG_PEER: Final = "the peer of the sandbox socket is not the sandbox's user"
BAD_RESPONSE: Final = "the answer of the sandbox service is not valid"
NO_COPY: Final = "the workspace couldn't be copied"
BAD_REQUEST: Final = "sandbox_bad_request"
WORKSPACE_REFUSED: Final = "sandbox_workspace_refused"

CONNECT_SECONDS: Final = 5.0
UPLOAD_SECONDS: Final = 60.0  # 7.5: the archive must arrive completely within 60 s
STOP_POLL_SECONDS: Final = 2.0  # R27: the watcher looks every 2 s
_CHUNK: Final = 65_536
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class _Failed(Exception):
    """A failed call (R55); the text is one of the fixed reasons above or a refusal code."""


class _Stopped(Exception):
    """The runner closed the connection itself (a stop request or a recorded halt)."""


def _member(name: str, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mode = stat.S_IMODE(mode) & 0o777  # set-uid, set-gid and sticky bits never travel
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    return info


def write_archive(copy: str, out: IO[bytes]) -> None:
    """Write the tar of the private copy `copy` to `out` (R57a). Raises `ArchiveRefused`.

    Nothing outside the copy is read: the walk goes down by directory handles opened with
    `O_NOFOLLOW`, one name at a time. A hard link is a regular member. There is no member for
    the root itself.
    """
    try:
        root = os.open(copy, _DIR_FLAGS)
    except OSError as error:
        raise ArchiveRefused("the copy is not a directory") from error
    stack: list[tuple[int, str, list[str]]] = []
    try:
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            stack.append((root, "", sorted(os.listdir(root), reverse=True)))
            while stack:
                fd, prefix, names = stack[-1]
                if not names:
                    os.close(fd)
                    stack.pop()
                    continue
                name = names.pop()
                path = prefix + name
                status = os.lstat(name, dir_fd=fd)
                if stat.S_ISLNK(status.st_mode):
                    info = _member(path, 0o777)
                    info.type = tarfile.SYMTYPE
                    info.linkname = os.readlink(name, dir_fd=fd)
                    tar.addfile(info)
                elif stat.S_ISDIR(status.st_mode):
                    child = os.open(name, _DIR_FLAGS, dir_fd=fd)
                    stack.append((child, path + "/", sorted(os.listdir(child), reverse=True)))
                    info = _member(path, os.fstat(child).st_mode)
                    info.type = tarfile.DIRTYPE
                    tar.addfile(info)
                elif stat.S_ISREG(status.st_mode):
                    with os.fdopen(os.open(name, _FILE_FLAGS, dir_fd=fd), "rb") as source:
                        opened = os.fstat(source.fileno())
                        if not stat.S_ISREG(opened.st_mode):
                            raise ArchiveRefused("a member that is not a file, directory or link")
                        info = _member(path, opened.st_mode)
                        info.size = opened.st_size
                        tar.addfile(info, source)
                else:
                    raise ArchiveRefused("a member that is not a file, directory or link")
    except (OSError, tarfile.TarError, ValueError) as error:
        # a file that became a link or shrank meanwhile, too many levels, an unreadable name
        raise ArchiveRefused("the copy couldn't be read") from error
    finally:
        for fd, _, _ in stack:
            try:
                os.close(fd)
            except OSError:
                pass


class RemoteSandbox:
    """A `Sandbox` backed by the sandbox service (module docstring).

    `peer_uid` is the sandbox's user id (`NURAMESH_SANDBOX_UID`); with None no peer passes the
    check, so nothing is ever sent. `stop` is asked every `stop_poll` seconds while a command
    runs; true, or an error while asking, closes the connection.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        peer_uid: int | None,
        stop: Callable[[], bool] | None = None,
        connect_timeout: float = CONNECT_SECONDS,
        upload_seconds: float = UPLOAD_SECONDS,
        stop_poll: float = STOP_POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.socket_path = socket_path
        self.peer_uid = peer_uid
        self.connect_timeout = connect_timeout
        self.last_probe: str | None = None
        self._stop, self._stop_poll = stop, stop_poll
        self._upload_seconds = upload_seconds
        self._clock = clock
        self._probed_at: float | None = None
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._in_flight: socket.socket | None = None

    # --- the socket ---------------------------------------------------------------------------

    def deadline_seconds(self, request: SandboxRequest) -> float:
        """R55: a complete answer must arrive within `timeout_seconds` plus the upload window."""
        return request.limits.timeout_seconds + self._upload_seconds

    def _connect(self) -> socket.socket:
        try:
            status = os.lstat(self.socket_path)
            directory = os.stat(os.path.dirname(self.socket_path))
        except OSError as error:
            raise _Failed(NOT_A_SOCKET) from error
        if not stat.S_ISSOCK(status.st_mode) or (status.st_mode | directory.st_mode) & 0o002:
            raise _Failed(NOT_A_SOCKET)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(self.connect_timeout)
            client.connect(self.socket_path)
            raw = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError as error:
            client.close()
            raise _Failed(NO_CONNECTION) from error
        peer = struct.unpack("3i", raw)[1]
        if self.peer_uid is None or peer != self.peer_uid:
            client.close()
            raise _Failed(WRONG_PEER)
        return client

    def _interrupted(self) -> bool:
        return self._cancelled.is_set()

    def _receive(self, client: socket.socket, n: int, end: float) -> bytes:
        """Exactly `n` bytes before `end`, or fewer when the stream ends first."""
        data = b""
        while len(data) < n:
            left = end - time.monotonic()
            if left <= 0:
                raise _Failed(NO_ANSWER)
            client.settimeout(left)
            try:
                chunk = client.recv(n - len(data))
            except TimeoutError as error:
                raise _Failed(NO_ANSWER) from error
            except OSError:
                chunk = b""  # a reset: the stream ended
            if not chunk:
                break
            data += chunk
        return data

    def _answer(
        self, client: socket.socket, op: Op, output_bytes: int, end: float
    ) -> tuple[str, SandboxResult | None]:
        prefix = self._receive(client, 4, end)
        try:
            body = self._receive(client, response_length(prefix), end)
            if self._receive(client, 1, end):
                raise WireError("bytes after the response")
            return decode_response(body, op=op, output_bytes=output_bytes)
        except WireError as error:
            raise _Failed(BAD_RESPONSE) from error

    # --- health (R41, R52) --------------------------------------------------------------------

    def health(self, timeout: float = PROBE_SECONDS) -> str:
        """One health probe: `ready`, a state code of the service, or `no answer`."""
        end = time.monotonic() + timeout
        code = NO_ANSWER
        try:
            client = self._connect()
        except _Failed:
            client = None
        if client is not None:
            try:
                client.settimeout(max(0.01, end - time.monotonic()))
                client.sendall(encode_request("health"))
                code = self._answer(client, "health", 0, end)[0]
            except (_Failed, OSError):
                code = NO_ANSWER
            finally:
                client.close()
        with self._lock:
            self.last_probe, self._probed_at = code, self._clock()
        return code

    def ready(self) -> bool:
        """R41: the last probe answered `ready`. Probes first when the last answer is older than
        30 s, or when there is none (R52)."""
        with self._lock:
            at = self._probed_at
        if at is None or self._clock() - at > PROBE_MAX_AGE_SECONDS:
            self.health()
        return runner_sandbox_ready(SERVICE, last_probe=self.last_probe)

    # --- one command --------------------------------------------------------------------------

    def cancel(self) -> None:
        """The run was stopped: close the connection of the command in flight (the supervisor
        kills it), and start no new one."""
        self._cancelled.set()
        self._cut()

    def _cut(self) -> None:
        with self._lock:
            client = self._in_flight
        if client is not None:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _watch(self, done: threading.Event, stopped: threading.Event) -> None:
        assert self._stop is not None
        while not done.wait(self._stop_poll):
            try:
                stop = bool(self._stop())
            except Exception:  # an unreadable stop or halt state is a stop (fail closed)
                stop = True
            if stop:
                stopped.set()
                self._cut()
                return

    def run(self, request: SandboxRequest, workspace: str) -> SandboxResult:
        """Run the request in the service against a copy of `workspace` (never raises)."""
        started = time.monotonic()

        def result(**kw: Any) -> SandboxResult:
            return SandboxResult(
                duration_ms=int((time.monotonic() - started) * 1000),
                isolation=Isolation.SERVICE,
                runtime=SERVICE_RUNTIME,
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
        try:  # the longest archive length: the real frame is never longer than this one
            encode_request("run", request, workspace_bytes=2**63)
        except WireError:
            return not_started(BAD_REQUEST)
        try:
            copy = _copy_workspace(workspace, _sandbox_root())
        except SandboxUnavailable as exc:
            return not_started(str(exc))
        except shutil.Error:  # a socket, FIFO, device or unreadable file in the workspace
            return not_started(WORKSPACE_REFUSED)
        except OSError:
            return not_started(NO_COPY)
        parent = os.path.dirname(copy)
        stopped = threading.Event()
        try:
            with tempfile.TemporaryFile(dir=parent) as archive:
                write_archive(copy, archive)
                size = archive.tell()
                archive.seek(0)
                shutil.rmtree(copy, ignore_errors=True)  # only the archive is needed from here
                code, answer = self._call(request, archive, size, stopped)
        except ArchiveRefused:
            return not_started(WORKSPACE_REFUSED)
        except _Stopped:
            return result(
                exit_code=None,
                timed_out=False,
                started=True,
                stderr_tail=CANCELLED_TEXT,
                cancelled=True,
            )
        except _Failed as failure:
            return not_started(str(failure))
        except OSError:
            return not_started(NO_COPY)
        finally:
            shutil.rmtree(parent, ignore_errors=True)
        if code != OK or answer is None:
            return not_started(code)  # a refusal code of the service, as a fixed code
        return answer

    def _call(
        self, request: SandboxRequest, archive: IO[bytes], size: int, stopped: threading.Event
    ) -> tuple[str, SandboxResult | None]:
        frame = encode_request("run", request, workspace_bytes=size)
        client = self._connect()
        done = threading.Event()
        watcher: threading.Thread | None = None
        try:
            with self._lock:
                self._in_flight = client
            if self._stop is not None:
                watcher = threading.Thread(target=self._watch, args=(done, stopped), daemon=True)
                watcher.start()
            end = time.monotonic() + self.deadline_seconds(request)
            try:
                self._send(client, frame, archive, end)
                return self._answer(client, "run", request.limits.output_bytes, end)
            except _Failed:
                if stopped.is_set() or self._cancelled.is_set():
                    raise _Stopped() from None
                raise
        finally:
            done.set()
            with self._lock:
                self._in_flight = None
            client.close()
            if watcher is not None:
                watcher.join(timeout=1.0)

    def _send(self, client: socket.socket, frame: bytes, archive: IO[bytes], end: float) -> None:
        """Send the frame and the archive. A service that answers early (a refusal) may close
        while bytes are still being sent: that is not a failure here, the answer decides."""
        try:
            chunk = frame
            while chunk:
                left = end - time.monotonic()
                if left <= 0:
                    raise _Failed(NO_ANSWER)
                client.settimeout(left)
                client.sendall(chunk)
                chunk = archive.read(_CHUNK)
        except TimeoutError as error:
            raise _Failed(NO_ANSWER) from error
        except OSError:
            return
