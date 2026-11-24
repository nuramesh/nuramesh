"""Start, stop and clean up the throwaway PostgreSQL server the tests use.

The server lives in a folder `<base>/<prefix>XXXX` holding `data/`, `sock/`, `log` and a marker
file the helpers themselves write. Only folders with that exact prefix, directly under the base
directory and carrying the marker are ever removed, and a folder whose server is still running
(another live test session) is never touched. The base and prefix are parameters so the tests of
these helpers never go near the real `/tmp/nuramesh-pg-*` folders.
"""

import atexit
import contextlib
import fcntl
import glob
import hashlib
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
from collections.abc import Iterator
from pathlib import Path

DEFAULT_PG_MAJOR = 16  # the major the CI pgvector check verifies (vector.control under 16)
LIB_DIR = "/usr/lib/postgresql"


INITDB_TIMEOUT = 120.0  # seconds; a stalled initdb must never hang a test worker
START_TIMEOUT = 60  # pg_ctl -t: how long `start -w` waits for the server
STOP_TIMEOUT = 60.0
QUICK_TIMEOUT = 30.0  # `--version`, `ps`


def run_bounded(
    cmd: list[str], timeout: float, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run `cmd` in its own process group with a deadline; on expiry kill the whole group.

    A timeout raises `subprocess.TimeoutExpired` (stderr captured so far is attached) and leaves
    no orphan of the command behind.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.communicate()
        raise
    done = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check:
        done.check_returncode()
    return done


def pg_major() -> int:
    """The PostgreSQL major the tests use: NURAMESH_PG_MAJOR, default 16."""
    return int(os.environ.get("NURAMESH_PG_MAJOR") or DEFAULT_PG_MAJOR)


def binary_major(path: str) -> int | None:
    """The major version a PostgreSQL binary reports (`pg_ctl (PostgreSQL) 16.4`), or None."""
    try:
        out = run_bounded([path, "--version"], QUICK_TIMEOUT).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", out)
    return int(match.group(1)) if match else None


def find_binary(name: str, lib_dir: str = LIB_DIR) -> str | None:
    """A PostgreSQL binary of the wanted major (`pg_major()`), or None.

    The Ubuntu package folder `<lib_dir>/<major>/bin` is used first; a binary on PATH or in
    Homebrew counts only when its own version reports the same major, so a second installed
    major can never be picked silently (see `mismatch_message`).
    """
    major = pg_major()
    pinned = Path(lib_dir) / str(major) / "bin" / name
    if pinned.is_file():
        return str(pinned)
    others = [shutil.which(name), *sorted(glob.glob(f"/opt/homebrew/opt/postgresql*/bin/{name}"))]
    for found in others:
        if found and binary_major(found) == major:
            return found
    return None


def mismatch_message(name: str, lib_dir: str = LIB_DIR) -> str:
    """Why `find_binary` found nothing: names the wanted major and any other major seen."""
    major = pg_major()
    seen = sorted(
        {
            m
            for f in [shutil.which(name), *glob.glob(f"{lib_dir}/*/bin/{name}")]
            if f and (m := binary_major(f)) is not None and m != major
        }
    )
    text = f"PostgreSQL {major} binary '{name}' isn't installed (NURAMESH_PG_MAJOR={major})"
    if seen:
        text += f"; found other major(s) {', '.join(map(str, seen))}"
    return text


PREFIX = "nuramesh-pg-"
BASE = os.environ.get("NURAMESH_PG_BASE") or "/tmp"  # short path (socket limit); guard sets it
MARKER = ".nuramesh-throwaway-pg"

# Throwaway settings: nothing is kept, nothing is replicated, memory and WAL stay small.
SERVER_OPTIONS = (
    "-c listen_addresses='' -c fsync=off -c synchronous_commit=off -c full_page_writes=off "
    "-c wal_level=minimal -c max_wal_senders=0 -c max_wal_size=32MB -c min_wal_size=32MB "
    "-c shared_buffers=16MB -c autovacuum=off -c max_connections=50 -c log_min_messages=warning"
)


# The cluster-wide role migration 28 checks (compose: deploy/compose/initdb/10-roles.sh).
DEFINER_ROLE_SQL = (
    "CREATE ROLE nuramesh_definer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
    "NOREPLICATION NOBYPASSRLS"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours: treat as live
    return True


def _pid_is_postgres(pid: int) -> bool:
    if not _alive(pid):
        return False
    try:
        text = run_bounded(["ps", "-p", str(pid), "-o", "stat=,comm="], QUICK_TIMEOUT).stdout
    except subprocess.TimeoutExpired:
        return False
    out = text.split(None, 1)
    if len(out) != 2 or out[0].startswith("Z"):
        return False  # gone, or a zombie nobody reaped (an orphan under a container's init)
    return "postgres" in out[1] or "postmaster" in out[1]


def server_pid(root: Path) -> int | None:
    """The postmaster pid from the folder's postmaster.pid, or None."""
    try:
        return int((root / "data" / "postmaster.pid").read_text().splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def server_running(root: Path) -> bool:
    """True when the folder's postmaster.pid names a live postgres process."""
    pid = server_pid(root)
    return pid is not None and _pid_is_postgres(pid)


def owner_alive(root: Path) -> bool:
    """True when the test session that created the folder (pid in the marker) still runs."""
    try:
        pid = int((root / MARKER).read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return True  # empty or unreadable: being written right now; leave the folder alone
    return _alive(pid)


def write_marker(root: Path, worker: str = "master") -> None:
    """Write the marker atomically: a reader sees no marker or the complete one, never a part.

    Line 1 is the owner pid (the only part the code reads); line 2 names the pytest-xdist worker
    (`gw2`, or `master` without xdist) so a leftover folder says whose it was (H-15).
    """
    temp = root / (MARKER + ".tmp")
    temp.write_text(f"{os.getpid()}\nworker={worker}\nwritten by tests/pg_throwaway.py\n")
    os.replace(temp, root / MARKER)


def _serves(pid: int, root: Path) -> bool:
    """True when `pid` is a postgres process started for this folder's own data directory."""
    if not _pid_is_postgres(pid):
        return False
    try:
        args = run_bounded(["ps", "-ww", "-p", str(pid), "-o", "args="], QUICK_TIMEOUT).stdout
    except subprocess.TimeoutExpired:
        return False
    # -ww: without it procps cuts the line at 80 columns when it cannot see a terminal
    return f"-D {root / 'data'}" in args


def is_ours(path: Path, base: str = BASE, prefix: str = PREFIX) -> bool:
    """True for a real directory directly under `base`, named `prefix*`, with our marker."""
    return (
        path.parent == Path(base)
        and path.name.startswith(prefix)
        and not path.is_symlink()
        and path.is_dir()
        and (path / MARKER).is_file()
    )


def stop_server(pg_ctl: str, root: Path) -> None:
    """Stop the folder's server immediately (no error if it is not running)."""
    try:
        run_bounded(
            [pg_ctl, "-D", str(root / "data"), "-m", "immediate", "-t", "30", "stop"], STOP_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        pass  # bounded: the caller removes the folder and the group was killed


def remove_stale(pg_ctl: str | None, base: str = BASE, prefix: str = PREFIX) -> list[Path]:
    """Delete our own leftover folders; return what was removed.

    A folder is a leftover when the session that created it (the pid in its marker) is gone. Its
    server, if still running (a killed run leaves it behind), is stopped first. A folder whose
    creating session is alive is never touched. Folders without our marker are never touched.
    """
    removed: list[Path] = []
    for path in sorted(Path(base).glob(prefix + "*")):
        if not is_ours(path, base, prefix) or owner_alive(path):
            continue
        pid = server_pid(path)
        # Signal only a process proven to serve this folder (a reused pid may be another postgres).
        if pg_ctl and pid is not None and _serves(pid, path):
            stop_server(pg_ctl, path)
            if _serves(pid, path):
                os.kill(pid, signal.SIGQUIT)  # immediate shutdown, as pg_ctl -m immediate
        shutil.rmtree(path, ignore_errors=True)
        removed.append(path)
    return removed


# ---- template cluster (A3): initdb and the definer role once per key, then copy per run ----

TEMPLATE_PREFIX = "nuramesh-pgtpl-"  # never PREFIX: the stale-folder sweep must not see it
TEMPLATE_OK = ".template-ok"
REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_FILES = ("src/nuramesh/stores/postgres.py", "src/nuramesh/stores/migration_names.txt")


def log_line(text: str) -> None:
    """The one visible line a template fallback prints (never silent)."""
    print(f"pg_throwaway: {text}", file=sys.stderr, flush=True)


def template_key(initdb: str, repo: Path = REPO_ROOT) -> str:
    """A short hash of what a template cluster depends on: the exact initdb version, the role SQL
    and the migration sources. Any change builds a new template; the old one is removed."""
    digest = hashlib.sha256()
    digest.update(run_bounded([initdb, "--version"], QUICK_TIMEOUT).stdout.encode())
    digest.update(DEFINER_ROLE_SQL.encode())
    for name in MIGRATION_FILES:
        try:
            digest.update((repo / name).read_bytes())
        except OSError:
            digest.update(b"missing:" + name.encode())
    return digest.hexdigest()[:12]


def template_base(base: str = BASE) -> str:
    """Where templates live: NURAMESH_PG_TEMPLATE_DIR, else the same base as the clusters."""
    return os.environ.get("NURAMESH_PG_TEMPLATE_DIR") or base


TEMPLATE_LOCK_TIMEOUT = 120.0  # seconds to wait for the template lock before falling back
TEMPLATE_KEEP_SECONDS = 6 * 3600  # a template of another key is pruned only when older than this
LEFTOVER_SECONDS = 3600  # build leftovers older than this are swept


def _uid() -> int:
    return os.getuid()


def _trusted(path: Path) -> bool:
    """True for a real directory (no symlink) owned by this user with no group/other access."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == _uid() and not info.st_mode & 0o077


def _build_template(initdb: str, pg_ctl: str, final: Path) -> None:
    """Build an initdb'd cluster with the definer role in a private (0700) temp folder next to
    `final`, then rename it into place."""
    import psycopg

    work = Path(tempfile.mkdtemp(prefix=final.name + ".tmp-", dir=final.parent))
    sockets = Path(tempfile.mkdtemp(prefix="nm-tpl-", dir=final.parent))
    data = work / "data"
    try:
        run_bounded(
            [initdb, "-D", str(data), "-A", "trust", "-U", "postgres", "-E", "UTF8"],
            INITDB_TIMEOUT,
            check=True,
        )
        options = f"{SERVER_OPTIONS} -c unix_socket_directories='{sockets}'"
        run_bounded(
            [pg_ctl, "-D", str(data), "-o", options, "-w", "-t", str(START_TIMEOUT)]
            + ["-l", str(work / "log"), "start"],
            START_TIMEOUT + 15,
            check=True,
        )
        try:
            with psycopg.connect(
                f"host={sockets} user=postgres dbname=postgres", autocommit=True
            ) as db:
                db.execute(DEFINER_ROLE_SQL)
        finally:
            run_bounded([pg_ctl, "-D", str(data), "-m", "fast", "-w", "stop"], STOP_TIMEOUT)
        (work / "log").unlink(missing_ok=True)
        (work / TEMPLATE_OK).write_text("ok\n")
        os.rename(work, final)  # atomic: a reader sees no template or the complete one
    except BaseException:
        stop_server(pg_ctl, work)
        shutil.rmtree(work, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(sockets, ignore_errors=True)


def _open_lock(where: Path) -> int:
    """Open (create, 0600) the template lock without following a symlink; it must be ours."""
    fd = os.open(where / f"{TEMPLATE_PREFIX}lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if info.st_uid != _uid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        os.close(fd)
        raise PermissionError("template lock file is not private to this user")
    return fd


def _flock(fd: int, mode: int, deadline: float) -> None:
    """flock with LOCK_NB polled until the deadline; TimeoutError when it never comes."""
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("template lock busy") from None
            time.sleep(0.05)


def _sweep_leftovers(where: Path, pg_ctl: str, now: float) -> None:
    """Remove our own build leftovers older than an hour (a killed build): stop a postmaster
    proven to serve the folder, then delete it. Called under the exclusive lock."""
    for path in [*where.glob(TEMPLATE_PREFIX + "*.tmp-*"), *where.glob("nm-tpl-*")]:
        try:
            if not _trusted(path) or now - path.lstat().st_mtime < LEFTOVER_SECONDS:
                continue
        except OSError:
            continue
        pid = server_pid(path)
        if pid is not None and _serves(pid, path):
            stop_server(pg_ctl, path)
            if _serves(pid, path):
                os.kill(pid, signal.SIGQUIT)
        shutil.rmtree(path, ignore_errors=True)


def _prune_old(where: Path, keep: Path, now: float) -> None:
    """Remove templates of other keys that have not been used for a long time (other worktrees
    with other migrations share the base, so a different key alone is no reason to delete)."""
    for other in where.glob(TEMPLATE_PREFIX + "*"):
        ok = other / TEMPLATE_OK
        if other == keep or not _trusted(other) or not ok.is_file():
            continue
        if now - ok.stat().st_mtime > TEMPLATE_KEEP_SECONDS:
            shutil.rmtree(other, ignore_errors=True)


@contextlib.contextmanager
def template_lease(
    initdb: str, pg_ctl: str, key: str, base: str | None = None, timeout: float | None = None
) -> Iterator[Path]:
    """Yield the template folder for `key`, holding a SHARED lock for the whole `with` body, so
    nobody prunes it mid-copy. The template is built once under the exclusive lock.

    Everything is checked before use: the lock file and the template must be real objects owned
    by this user with mode 0700 or 0600 (the default base /tmp is world-writable and the name
    is predictable). A planted symlink raises; our own template with a bad mode is rebuilt; a
    foreign-owned one raises. The caller falls back to plain initdb on any exception.
    """
    where = Path(base if base is not None else template_base())
    where.mkdir(parents=True, exist_ok=True)
    final = where / f"{TEMPLATE_PREFIX}{key}"
    deadline = time.monotonic() + (TEMPLATE_LOCK_TIMEOUT if timeout is None else timeout)
    fd = _open_lock(where)
    try:
        _flock(fd, fcntl.LOCK_EX, deadline)
        now = time.time()
        _sweep_leftovers(where, pg_ctl, now)
        ready = (final / TEMPLATE_OK).is_file() and _trusted(final)
        if not ready:
            try:
                info = final.lstat()
            except FileNotFoundError:
                pass
            else:
                if stat.S_ISLNK(info.st_mode):
                    final.unlink()
                elif info.st_uid != _uid():
                    raise PermissionError(f"{final} is owned by another user")
                else:
                    shutil.rmtree(final)
            _build_template(initdb, pg_ctl, final)
        else:
            os.utime(final / TEMPLATE_OK)  # mark as used: pruning goes by this
        _prune_old(where, final, now)
        # Downgrade to shared for the copy. flock conversion is NOT atomic on Linux (the exclusive
        # lock is dropped, then the shared one is taken), so a prune or rebuild by another run
        # can land in between: re-check the template after the downgrade, and raise (the caller
        # falls back to plain initdb) if it is gone.
        _flock(fd, fcntl.LOCK_SH, deadline)
        if not ((final / TEMPLATE_OK).is_file() and _trusted(final)):
            raise RuntimeError("template changed while the lock was downgraded")
        yield final
    finally:
        os.close(fd)  # drops the lock


def ensure_template(initdb: str, pg_ctl: str, key: str, base: str | None = None) -> Path:
    """Build (if needed) and return the template folder for `key` (no lock is kept; use
    `template_lease` when copying)."""
    with template_lease(initdb, pg_ctl, key, base) as path:
        return path


def copy_template(template: Path, data: Path) -> None:
    """Copy the template's data directory to `data` (reflink when the filesystem has it). The
    template is only read. A failed copy (disk full) is retried with a plain copy, then raises.

    `--reflink=auto` is a GNU coreutils option (Linux). On any other platform (macOS `cp` has no
    such option) the `cp` call is skipped and the slower plain copy is used, with one log line.
    """
    if sys.platform.startswith("linux"):
        done = subprocess.run(
            ["cp", "-a", "--reflink=auto", str(template / "data"), str(data)],
            capture_output=True,
            text=True,
        )
        failed = done.returncode != 0
    else:
        log_line(f"cp --reflink is GNU-only; copying the template with shutil on {sys.platform}")
        failed = True
    if failed:
        shutil.rmtree(data, ignore_errors=True)
        shutil.copytree(template / "data", data, symlinks=True)
    data.chmod(0o700)


def start(
    initdb: str,
    pg_ctl: str,
    base: str = BASE,
    prefix: str = PREFIX,
    worker: str = "master",
    template_dir: str | None = None,
) -> tuple[Path, str]:
    """Clean our stale folders, then create and start a server; return (root, socket dir).

    `worker` is the pytest-xdist worker id recorded in the marker (never used for a path: the
    folder name comes from `mkdtemp`, unique per call). With `template_dir` the data directory is
    copied from a template cluster (see `ensure_template`); a template that fails falls back to a
    plain initdb with one log line.
    """
    remove_stale(pg_ctl, base, prefix)
    root = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    write_marker(root, worker)
    data, sockets = root / "data", root / "sock"
    sockets.mkdir()
    try:
        copied = False
        if template_dir is not None:
            try:
                with template_lease(initdb, pg_ctl, template_key(initdb), template_dir) as template:
                    copy_template(template, data)
                copied = True
            except Exception as exc:  # any failure: fall back, never skip, never silent
                log_line(f"template cluster unusable ({exc!r}); falling back to initdb")
                shutil.rmtree(data, ignore_errors=True)
        if not copied:
            run_bounded(
                [initdb, "-D", str(data), "-A", "trust", "-U", "postgres", "-E", "UTF8"],
                INITDB_TIMEOUT,
                check=True,
            )
        options = f"{SERVER_OPTIONS} -c unix_socket_directories='{sockets}'"
        run_bounded(
            [pg_ctl, "-D", str(data), "-o", options, "-w", "-t", str(START_TIMEOUT)]
            + ["-l", str(root / "log"), "start"],
            START_TIMEOUT + 15,
            check=True,
        )
    except BaseException:
        stop_server(pg_ctl, root)
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root, str(sockets)


class Cleanup:
    """Idempotent stop-and-delete, wired to normal exit, atexit and SIGTERM."""

    def __init__(self, pg_ctl: str, root: Path) -> None:
        self.pg_ctl, self.root = pg_ctl, root
        self.done = False
        self._previous: object = None
        self._installed = False

    def run(self) -> None:
        if self.done:
            return
        self.done = True
        stop_server(self.pg_ctl, self.root)
        shutil.rmtree(self.root, ignore_errors=True)

    def _on_sigterm(self, signum: int, frame: types.FrameType | None) -> None:
        self.run()
        previous = self._previous
        signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)  # type: ignore[arg-type]
        os.kill(os.getpid(), signal.SIGTERM)  # let the previous behaviour finish the job

    def install(self) -> None:
        atexit.register(self.run)
        try:
            self._previous = signal.signal(signal.SIGTERM, self._on_sigterm)
            self._installed = True
        except ValueError:  # not the main thread
            self._installed = False

    def uninstall(self) -> None:
        atexit.unregister(self.run)
        if self._installed:
            signal.signal(
                signal.SIGTERM,
                self._previous if self._previous is not None else signal.SIG_DFL,  # type: ignore[arg-type]
            )
            self._installed = False


def ensure_definer_role(sockets: str) -> None:
    """Create the cluster-wide definer role unless the template cluster already has it."""
    import psycopg

    with psycopg.connect(f"host={sockets} user=postgres dbname=postgres", autocommit=True) as db:
        row = db.execute("SELECT 1 FROM pg_roles WHERE rolname = 'nuramesh_definer'").fetchone()
        if row is None:
            db.execute(DEFINER_ROLE_SQL)
