"""The supervisor: socket, peer check, health, admission, one command at a time (R43 to R59).

Two threads, both from the start: the **front** thread accepts connections, checks the peer's
user id, reads the request frame and answers `health` at once from the cached state; the **main**
thread computes the state and runs the commands, one at a time. No thread and no process other
than the command's own is created while a command runs, so a command that uses every process
slot cannot stop a health answer or the kill (R56).
"""

import os
import platform
import queue
import selectors
import shlex
import shutil
import signal
import socket
import stat
import struct
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from nuramesh.core.sandbox import (
    SERVICE_RUNTIME,
    Isolation,
    SandboxLimits,
    SandboxRequest,
    SandboxResult,
)
from nuramesh.core.sandbox_seccomp import command_filter
from nuramesh.core.sandbox_service import (
    OK,
    ArchiveRefused,
    CgroupFiles,
    RequestOp,
    ServiceState,
    WireError,
    admit,
    cut_tail,
    decode_request,
    encode_response,
    request_length,
    service_state,
    status_field,
)
from nuramesh.sandboxd.launch import Outcome, adopt_orphans, run_command
from nuramesh.sandboxd.unpack import remove_tree, unpack_archive

STATE_MAX_AGE = 30.0
SELFTEST_SECONDS = 10.0
HEADER_SECONDS = 5.0
ARCHIVE_SECONDS = 60.0
ARCHIVE_WINDOW_SECONDS = 10.0
ARCHIVE_WINDOW_BYTES = 1_048_576
SETTLE_SECONDS = 5.0
MAX_WAITING = 16
TMP_BYTES = 64 * 1_048_576
PROBE = "/bin/nuramesh-sandbox-probe"

# Every line the self-test of R52 needs from the probe (`probe.c`, mode `selftest`).
SELFTEST_REQUIRED = (
    "caps.eff_zero",
    "nnp.on",
    "seccomp.mode_filter",
    "seccomp.filters_at_least_2",  # the profile of R47 and the command's own filter (S16s)
    "profile.personality_refused",  # only the pinned profile refuses it (S16s2)
    "sc.unshare0_EPERM",  # the command's own filter is on
    "sc.unshare_newuser",
    "sc.mount",
    "net.only_lo",
    "fd.only_0_1_2",
    "pid.only_own_tree",
    "fs.root_listing_is_rootfs",
    "fs.absent:/sys",
    "fs.absent:/run/sandbox",
    "fs.absent:/images",
    "fs.absent:/work",
    "fs.root_readonly",
    "fs.usr_readonly",
)


def selftest_failures(output: str) -> tuple[str, ...]:
    """What is wrong with the probe's output, by check name only (no detail text); empty = passed.

    `failed <check>`: a line of that check does not say PASS, or the check is reported twice;
    `missing <check>`: a needed check has no line; `the probe did not finish`: no end line.
    """
    verdicts: dict[str, list[str]] = {}
    done = False
    for line in output.splitlines():
        words = line.split(" ", 3)
        if words[0] == "R" and len(words) >= 3:
            verdicts.setdefault(words[1], []).append(words[2])
        elif line == "I done 1":
            done = True
    found = [f"failed {check}" for check, said in verdicts.items() if said != ["PASS"]]
    found += [f"missing {check}" for check in SELFTEST_REQUIRED if check not in verdicts]
    if not done:
        found.append("the probe did not finish")
    return tuple(found)


def selftest_passed(output: str) -> bool:
    """The probe's output passes: every needed line says PASS, no line says FAIL, it finished."""
    return not selftest_failures(output)


def _log(text: str) -> None:
    """One line on stderr. Never request content, output or environment values."""
    sys.stderr.write(f"sandboxd: {text}\n")
    sys.stderr.flush()


@dataclass(frozen=True)
class Config:
    """Settings from the environment (`NURAMESH_SANDBOX_*`). The last five are for tests."""

    socket_path: str
    client_uid: int
    workspace_max_mb: int = 256
    bwrap: tuple[str, ...] = ("bwrap",)
    work: str = "/work"
    images: tuple[str, ...] = ("/images",)
    selftest_rootfs: str = "/opt/nuramesh/selftest-rootfs"
    cgroup: str = "/sys/fs/cgroup"
    proc_self: str = "/proc/self"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Config":
        """Read the settings; a missing or invalid one stops the start with exit code 2."""

        def fail(text: str) -> "Config":
            _log(text)
            raise SystemExit(2)

        path = env.get("NURAMESH_SANDBOX_SOCKET", "/run/sandbox/sandbox.sock")
        if not os.path.isabs(path):
            return fail("NURAMESH_SANDBOX_SOCKET must be an absolute path")
        uid = env.get("NURAMESH_SANDBOX_CLIENT_UID", "")
        if not (uid.isascii() and uid.isdigit()):
            return fail("NURAMESH_SANDBOX_CLIENT_UID (the runner's user id) must be set")
        max_mb = env.get("NURAMESH_SANDBOX_WORKSPACE_MAX_MB", "256")
        if not (max_mb.isascii() and max_mb.isdigit() and 1 <= int(max_mb) <= 4096):
            return fail("NURAMESH_SANDBOX_WORKSPACE_MAX_MB must be 1 to 4096")
        bwrap = shlex.split(env.get("NURAMESH_SANDBOX_BWRAP", "bwrap"))
        found = shutil.which(bwrap[0], path=env.get("PATH", os.defpath)) if bwrap else None
        if found is None:
            return fail("bwrap was not found")
        return cls(
            socket_path=path,
            client_uid=int(uid),
            workspace_max_mb=int(max_mb),
            bwrap=(os.path.abspath(found), *bwrap[1:]),
            work=env.get("NURAMESH_SANDBOX_WORK", "/work"),
            images=tuple(p for p in env.get("NURAMESH_SANDBOX_IMAGES", "/images").split(":") if p),
            selftest_rootfs=env.get(
                "NURAMESH_SANDBOX_SELFTEST_ROOTFS", "/opt/nuramesh/selftest-rootfs"
            ),
            cgroup=env.get("NURAMESH_SANDBOX_CGROUP", "/sys/fs/cgroup"),
            proc_self=env.get("NURAMESH_SANDBOX_PROC_SELF", "/proc/self"),
        )


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="ascii") as handle:
            return handle.read(65536)
    except (OSError, ValueError):
        return None


def _real_dir(path: str) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def usable_rootfs(rootfs: str) -> bool:
    """R56: a real directory in which `proc`, `dev`, `tmp` and `workspace` are real directories."""
    return _real_dir(rootfs) and all(
        _real_dir(os.path.join(rootfs, name)) for name in ("proc", "dev", "tmp", "workspace")
    )


class _Slow(Exception):
    """The archive did not arrive in time, or the client closed: close without an answer."""


@dataclass
class _Pending:
    data: bytes = b""
    need: int | None = None
    deadline: float = field(default_factory=lambda: time.monotonic() + HEADER_SECONDS)


class Supervisor:
    """The service. `serve` returns after `stop` (SIGTERM)."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._state = ServiceState("sandbox_selftest_failed", None)
        self._state_at = float("-inf")
        self._why = ""  # why the self-test failed (check names only), for the log
        self._logged: tuple[str, str] | None = None
        self._broken = False  # a command could not be cleaned up: nothing runs until restart
        self._stop = threading.Event()
        self._runs: queue.Queue[tuple[socket.socket, RequestOp]] = queue.Queue()
        self._waiting = 0
        self._lock = threading.Lock()
        self._copy_number = 0
        self._filter = b""
        self._listener: socket.socket | None = None

    # --- state (main thread) ---------------------------------------------------------------

    def _pids_current(self) -> int | None:
        text = _read(os.path.join(self.config.cgroup, "pids.current"))
        return int(text) if text is not None and text.strip().isdigit() else None

    def _reap(self) -> None:
        """Reap adopted children (process 1 of its container, or `adopt_orphans` elsewhere)."""
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            pass

    def _settled(self, baseline: int | None, seconds: float) -> bool:
        """`pids.current` is back at the count from before the command, within `seconds`."""
        end = time.monotonic() + seconds
        while True:
            self._reap()
            now = self._pids_current()
            if baseline is not None and now is not None and now <= baseline:
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)

    def _new_copy(self) -> str:
        self._copy_number += 1
        path = os.path.join(self.config.work, f"copy-{self._copy_number}")
        os.mkdir(path, 0o700)
        return path

    def _delete_copy(self, path: str) -> bool:
        try:
            fd = os.open(self.config.work, os.O_RDONLY | os.O_DIRECTORY)
            try:
                remove_tree(fd, os.path.basename(path))
            finally:
                os.close(fd)
        except OSError:
            return not os.path.lexists(path)
        return True

    def _launch(
        self,
        rootfs: str,
        copy: str,
        command: tuple[str, ...],
        env: Mapping[str, str],
        timeout: float,
        output_bytes: int,
        client: socket.socket | None = None,
    ) -> Outcome:
        return run_command(
            bwrap=self.config.bwrap,
            rootfs=rootfs,
            copy=copy,
            filter_bytes=self._filter,
            command=command,
            env=env,
            timeout=timeout,
            output_bytes=output_bytes,
            tmp_bytes=TMP_BYTES,
            client=client,
            stopping=self._stop.is_set,
        )

    def _selftest(self) -> bool:
        """R52 (3): the probe, `/bin/true` and a killed sleeper, all through the R56 line.

        On a failure `_why` names the step or the probe's checks, never any output text.
        """
        try:
            self._why = self._selftest_problem()
        except Exception as error:  # a self-test that cannot be made is a failed self-test
            self._why = f"could not run ({type(error).__name__})"
        return not self._why

    def _selftest_problem(self) -> str:
        """Empty when the self-test passed, else what failed."""
        end = time.monotonic() + SELFTEST_SECONDS
        rootfs = self.config.selftest_rootfs
        self._filter = command_filter(platform.machine())
        if not usable_rootfs(rootfs):
            return "the built-in root filesystem is not usable"
        listing = " ".join(sorted(n for n in os.listdir(rootfs) if not n.startswith(".")))
        steps: tuple[tuple[str, tuple[str, ...], float | None], ...] = (
            ("probe", (PROBE, "selftest", listing), None),
            ("true", ("/bin/true",), None),
            ("sleeper", (PROBE, "sleep-tree"), 0.5),
        )
        for step, command, limit in steps:
            left = end - time.monotonic()
            if left <= 0.6:
                return f"out of time before the {step} step"
            baseline = self._pids_current()
            copy = self._new_copy()
            outcome: Outcome | None = None
            try:
                outcome = self._launch(
                    rootfs, copy, command, {"PATH": "/bin"}, limit or left, 131_072
                )
            finally:
                # processes of a bwrap that was not reaped may still use the copy: keep it
                deleted = (
                    True
                    if outcome is not None and not outcome.reaped
                    else (self._delete_copy(copy))
                )
            if not deleted:
                return f"the copy of the {step} step was not deleted"
            if not outcome.started:
                return f"the {step} step did not start (bwrap could not run the command)"
            if not outcome.reaped:
                return f"bwrap of the {step} step did not end after the kill"
            if limit is None:
                if outcome.exit_code != 0:
                    return f"the {step} step ended with {outcome.exit_code}"
                if step == "probe":
                    failures = selftest_failures(outcome.stdout.decode("utf-8", "replace"))
                    if failures:
                        return ", ".join(failures)
            elif not outcome.timed_out:
                return f"the {step} step was not ended by its time limit"
            if not self._settled(baseline, max(0.0, min(SETTLE_SECONDS, end - time.monotonic()))):
                return f"processes were left after the {step} step"
        return ""

    def _recompute(self) -> None:
        cg, proc = self.config.cgroup, self.config.proc_self
        swap_path = os.path.join(cg, "memory.swap.max")
        files = CgroupFiles(
            memory_max=_read(os.path.join(cg, "memory.max")),
            cpu_max=_read(os.path.join(cg, "cpu.max")),
            pids_max=_read(os.path.join(cg, "pids.max")),
            memory_swap_max=_read(swap_path),
            swap_file_exists=os.path.lexists(swap_path),
        )
        why = ""
        if self._broken:
            state = ServiceState("sandbox_selftest_failed", None)
            why = "a command could not be cleaned up"
        else:
            status = _read(os.path.join(proc, "status"))
            self._why = ""
            state = service_state(
                files,
                _read(os.path.join(proc, "uid_map")),
                selftest=self._selftest,
                supervisor_status=status,
            )
            if state.code == "sandbox_selftest_failed":
                seccomp = status_field(status, "Seccomp")
                if self._why:
                    why = f"self-test: {self._why}"
                else:  # the self-test never ran: no seccomp profile is loaded in the container
                    value = "unreadable" if seccomp is None else str(seccomp)
                    why = f"the supervisor's Seccomp is {value}, needs 2"
        if (state.code, why) != self._logged:
            _log(f"state {state.code}" + (f": {why}" if why else ""))
            self._logged = (state.code, why)
        self._state, self._state_at = state, time.monotonic()

    # --- one run request (main thread) -----------------------------------------------------

    def _rootfs(self, request: SandboxRequest) -> str | None:
        """R50: the store directory named by the digest, or None."""
        digest = request.image.rsplit("@sha256:", 1)[1]
        for store in self.config.images:
            base = os.path.join(store, f"sha256-{digest}")
            rootfs = os.path.join(base, "rootfs")
            try:
                marker = stat.S_ISREG(os.lstat(os.path.join(base, "complete")).st_mode)
            except OSError:
                marker = False
            if marker and _real_dir(base) and usable_rootfs(rootfs):
                return rootfs
        return None

    @staticmethod
    def _answer(client: socket.socket, code: str, result: SandboxResult | None = None) -> None:
        try:
            client.settimeout(5)
            client.sendall(encode_response(code, result))
        except (OSError, WireError):
            pass

    @staticmethod
    def _archive_reader(client: socket.socket):  # type: ignore[no-untyped-def]
        """`read(n)` from the client: all within 60 s and at least 1 MiB in every 10 s."""
        end = time.monotonic() + ARCHIVE_SECONDS
        window = [time.monotonic(), 0]

        def read(n: int) -> bytes:
            now = time.monotonic()
            if now - window[0] >= ARCHIVE_WINDOW_SECONDS:
                if window[1] < ARCHIVE_WINDOW_BYTES:
                    raise _Slow
                window[0], window[1] = now, 0
            left = min(end - now, window[0] + ARCHIVE_WINDOW_SECONDS - now)
            if end - now <= 0:
                raise _Slow
            client.settimeout(max(left, 0.01))
            try:
                data = client.recv(n)
            except OSError:
                if time.monotonic() >= end or window[1] < ARCHIVE_WINDOW_BYTES:
                    raise _Slow from None
                return read(n)
            if not data:
                raise _Slow
            window[1] += len(data)
            return data

        return read

    def _refuse_early(self, client: socket.socket, code: str, declared: int) -> None:
        """Answer before the archive was read, then read what the client still sends (bounded)."""
        self._answer(client, code)
        end = time.monotonic() + 2.0
        left = min(declared, 4 * 1_048_576)
        try:
            client.shutdown(socket.SHUT_WR)
            while left > 0 and time.monotonic() < end:
                client.settimeout(0.5)
                data = client.recv(min(left, 65536))
                if not data:
                    break
                left -= len(data)
        except OSError:
            pass

    def _handle_run(self, client: socket.socket, op: RequestOp) -> None:
        request = op.request
        assert request is not None
        if time.monotonic() - self._state_at > STATE_MAX_AGE:
            self._recompute()  # outside the command's own time limit (R52)
        state = self._state
        rootfs = self._rootfs(request)

        def code(archive_ok: bool = True) -> str:
            return admit(
                state,
                limits=request.limits,
                image_in_store=rootfs is not None,
                workspace_bytes=op.workspace_bytes,
                workspace_max_mb=self.config.workspace_max_mb,
                archive_ok=archive_ok,
            )

        if code() != OK or rootfs is None:
            self._refuse_early(client, code(), op.workspace_bytes)
            return
        copy = self._new_copy()
        outcome: Outcome | None = None
        digest = ""
        try:
            fd = os.open(copy, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                digest = unpack_archive(
                    self._archive_reader(client),
                    op.workspace_bytes,
                    fd,
                    self.config.workspace_max_mb * 1_048_576,
                )
            except ArchiveRefused:
                self._answer(client, code(archive_ok=False))
                return
            except _Slow:
                return
            finally:
                os.close(fd)
            baseline = self._pids_current()
            outcome = self._launch(
                rootfs,
                copy,
                request.command,
                request.env,
                float(request.limits.timeout_seconds),
                request.limits.output_bytes,
                client,
            )
        finally:
            # A bwrap that was not reaped may still have processes of the command: they could
            # change the copy while it is deleted, so it stays for the next start (`serve`).
            deleted = (outcome is None or outcome.reaped) and self._delete_copy(copy)
            if outcome is not None or not deleted:
                clean = deleted and (outcome is None or outcome.reaped)
                if not (clean and (outcome is None or self._settled(baseline, SETTLE_SECONDS))):
                    self._broken = True
                    self._state = ServiceState("sandbox_selftest_failed", None)
                    _log("a command could not be cleaned up: nothing runs until restart")
        if outcome.cancelled:
            return  # the client is gone (or sent bytes it must not send): no answer
        self._answer(client, OK, self._result(request.limits, outcome, digest))

    @staticmethod
    def _result(limits: SandboxLimits, outcome: Outcome, digest: str) -> SandboxResult:
        out = cut_tail(outcome.stdout, limits.output_bytes)
        err = cut_tail(outcome.stderr, limits.output_bytes)
        cut = any(
            len(raw.decode("utf-8", "replace")) != len(text)
            for raw, text in ((outcome.stdout, out), (outcome.stderr, err))
        )
        return SandboxResult(
            exit_code=outcome.exit_code,
            timed_out=outcome.timed_out,
            duration_ms=outcome.duration_ms,
            stdout_tail=out,
            stderr_tail=err,
            isolation=Isolation.SERVICE,
            runtime=SERVICE_RUNTIME,
            started=outcome.started,
            output_truncated=outcome.truncated or cut,
            workspace_sha256=digest,
        )

    # --- the front thread ------------------------------------------------------------------

    def _peer_ok(self, conn: socket.socket) -> bool:
        try:
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError:
            return False
        _pid, peer_uid, _gid = struct.unpack("3i", raw)
        return int(peer_uid) == self.config.client_uid

    def _drop(self, selector: selectors.BaseSelector, conn: socket.socket) -> None:
        selector.unregister(conn)
        conn.close()
        with self._lock:
            self._waiting -= 1

    def _frame_done(
        self, selector: selectors.BaseSelector, conn: socket.socket, body: bytes
    ) -> None:
        try:
            op = decode_request(body)
            if op.op == "health":
                try:
                    if conn.recv(1, socket.MSG_PEEK):
                        raise WireError("bytes after a health request")
                except BlockingIOError:
                    pass
        except (WireError, OSError, ValueError):
            conn.setblocking(True)
            self._answer(conn, "sandbox_bad_request")
            self._drop(selector, conn)
            return
        if op.op == "health":
            conn.setblocking(True)
            self._answer(conn, self._state.code)
            self._drop(selector, conn)
            return
        selector.unregister(conn)
        conn.setblocking(True)
        self._runs.put((conn, op))  # still counted as waiting until the main thread takes it

    def _serve_key(
        self, selector: selectors.BaseSelector, listener: socket.socket, key: selectors.SelectorKey
    ) -> None:
        """One readable socket of the front thread: accept, or read more of a request frame."""
        if key.data is None:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with self._lock:
                full = self._waiting >= MAX_WAITING
                if not full:
                    self._waiting += 1
            if full or not self._peer_ok(conn):
                conn.close()  # R59: no answer
                if not full:
                    with self._lock:
                        self._waiting -= 1
                return
            conn.setblocking(False)
            selector.register(conn, selectors.EVENT_READ, _Pending())
            return
        pending = key.data
        assert isinstance(key.fileobj, socket.socket)
        conn = key.fileobj
        want = (4 if pending.need is None else pending.need) - len(pending.data)
        try:
            data = conn.recv(want)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            self._drop(selector, conn)
            return
        pending.data += data
        if pending.need is None and len(pending.data) == 4:
            try:
                pending.need = request_length(pending.data)
            except WireError:
                conn.setblocking(True)
                self._answer(conn, "sandbox_bad_request")
                self._drop(selector, conn)
                return
            pending.data = b""
        elif pending.need is not None and len(pending.data) == pending.need:
            self._frame_done(selector, conn, pending.data)

    def _front(self, listener: socket.socket) -> None:
        selector = selectors.DefaultSelector()
        selector.register(listener, selectors.EVENT_READ, None)
        while not self._stop.is_set():
            for key, _ in selector.select(0.25):
                try:
                    self._serve_key(selector, listener, key)
                except Exception as error:  # nothing a peer sends may end this thread
                    _log(f"a connection could not be served ({type(error).__name__}): closed")
                    conn = key.fileobj
                    try:  # still registered: it was neither dropped nor handed to the main thread
                        if key.data is not None and conn in selector.get_map():
                            assert isinstance(conn, socket.socket)
                            self._drop(selector, conn)
                    except (OSError, KeyError, ValueError):
                        pass
            now = time.monotonic()
            for key in list(selector.get_map().values()):
                if key.data is not None and now > key.data.deadline:
                    assert isinstance(key.fileobj, socket.socket)
                    self._drop(selector, key.fileobj)
        selector.close()

    # --- start and stop --------------------------------------------------------------------

    def _listen(self) -> socket.socket:
        path = self.config.socket_path
        try:
            if stat.S_ISSOCK(os.lstat(path).st_mode):
                os.unlink(path)  # left by an earlier start
        except FileNotFoundError:
            pass
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o117)  # the socket is 0660 from the moment it exists (R44)
        try:
            listener.bind(path)
        finally:
            os.umask(old)
        listener.listen(MAX_WAITING)
        return listener

    def stop(self) -> None:
        """Ask `serve` to return; a command in flight is killed."""
        self._stop.set()

    def serve(self) -> None:
        """Compute the state, listen, and serve until `stop`."""
        if not adopt_orphans() and os.getpid() != 1:
            _log("orphans are not adopted here: a killed command may stay a zombie")
        for name in os.listdir(self.config.work):  # copies left by a killed supervisor
            self._delete_copy(os.path.join(self.config.work, name))
        self._recompute()
        listener = self._listen()
        front = threading.Thread(target=self._front, args=(listener,), name="front", daemon=True)
        front.start()
        try:
            while not self._stop.is_set():
                try:
                    client, op = self._runs.get(timeout=0.25)
                except queue.Empty:
                    self._reap()
                    if time.monotonic() - self._state_at > STATE_MAX_AGE:
                        self._recompute()
                    continue
                with self._lock:
                    self._waiting -= 1
                try:
                    self._handle_run(client, op)
                finally:
                    client.close()
        finally:
            self._stop.set()
            front.join(2)
            listener.close()
            try:
                os.unlink(self.config.socket_path)
            except OSError:
                pass


def main() -> int:
    """Entry point of `python -m nuramesh.sandboxd`."""
    supervisor = Supervisor(Config.from_env(os.environ))
    signal.signal(signal.SIGTERM, lambda *_: supervisor.stop())
    signal.signal(signal.SIGINT, lambda *_: supervisor.stop())
    supervisor.serve()
    return 0
