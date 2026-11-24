"""pytest hooks for the test-run guard (ND119): node-id report, open-handle check, memory trace.

Wired in by `tests/conftest.py` (which imports the names below, so pytest registers them) and
loadable as a plugin (`-p guard_hooks` with `tests/` on PYTHONPATH) for the tests of this file.

* NODE ID. When `NURAMESH_GUARD_NODEID_FILE` is set (the runguard launcher sets it), the id
  of the test that is starting is written there, so a watchdog kill can name the test. Under
  pytest-xdist each worker writes `<file>.<workerid>` instead (H-20), so workers never overwrite
  each other and the kill message can name the test running on every worker.
* OPEN HANDLES, three states of `NURAMESH_FD_CHECK`: unset or any other value = off (nothing is
  checked or logged); `report` = every module's leaked database handles are logged and never fail
  a test (a GitHub annotation per leaking module, a Markdown table appended to the file named by
  GITHUB_STEP_SUMMARY, and a terminal summary at the end); `1` = blocking. Checked after the last
  test of a module and its module fixtures: no database file (`.db`, `.sqlite`, `-wal`, `-shm`,
  ...) opened during the module may still be open (garbage is collected first). In blocking mode
  a module in KNOWN_LEAKS (empty on purpose) may leak, with a reason; it is a ratchet: when such
  a module stops leaking the check fails until the entry is removed.
* MEMORY TRACE (opt-in: `MEMTRACE_LOG=<file>`). One CSV row per test module: process RSS after
  the module, the change from the module before, and the peak so far (MB). Under pytest-xdist
  each worker appends to `<file>.<workerid>` and the controller merges them into `<file>` at the
  end of the session (H-21).
* WRITE CHECK (opt-in: `NURAMESH_WRITE_CHECK=1`). After the last test of a module, no file of the
  checkout may be new or changed (tests write under `tmp_path`; H-5).

Everything that talks to pytest-xdist is guarded by `hasattr(config, "workerinput")` or
`hasattr(node, "workeroutput")` and does nothing in a plain run.
"""

from __future__ import annotations

import gc
import json
import os
import re
import resource
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

DB_SUFFIXES = (".db", ".sqlite", ".sqlite3", "-wal", "-shm", "-journal")

# Empty on purpose (#293): a module that leaves database files open fails the check. Close the
# store, connection or app in the fixture; a product leak is a bug, fix it. Do not park a module
# here: an entry needs a tracking issue. Key format if one is ever allowed:
# the rootdir-relative test file WITHOUT .py (the CI import graph reads a string naming a
# test_*.py file as an import of it) -> why it leaks. Ratchet: remove the entry when fixed.
KNOWN_LEAKS: dict[str, str] = {}


def open_database_files() -> list[str]:
    """Paths of the database files this process has open (empty where fds cannot be resolved)."""
    paths: list[str] = []
    try:
        fds = [int(name) for name in os.listdir("/dev/fd")]
    except (OSError, ValueError):
        return paths
    for fd in fds:
        path = _fd_path(fd)
        if path and path.endswith(DB_SUFFIXES):
            paths.append(path)
    return sorted(paths)


def _fd_path(fd: int) -> str | None:
    if sys.platform == "darwin":
        try:
            import fcntl

            raw = fcntl.fcntl(fd, 50, b"\0" * 1024)  # F_GETPATH
        except OSError:
            return None
        return raw.split(b"\0", 1)[0].decode("utf-8", "replace") or None
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return None


def judge(module: str, leaked: list[str], allow: Mapping[str, str] | None = None) -> str | None:
    """The failure message for a module, or None when it is fine (the ratchet lives here)."""
    known = KNOWN_LEAKS if allow is None else allow
    if leaked and module not in known:
        shown = "\n  ".join(leaked[:10])
        return (
            f"{module} left database files open after its last test ({len(leaked)}):\n  {shown}\n"
            "Close the store, connection or app in the fixture (yield, then close). If it is a "
            "product bug, fix it; only a tracked, justified leak goes in KNOWN_LEAKS "
            "(tests/guard_hooks.py)."
        )
    if not leaked and module in known:
        return (
            f"{module} no longer leaks database files: remove it from KNOWN_LEAKS in "
            "tests/guard_hooks.py (the allowlist only shrinks)."
        )
    return None


_findings: list[tuple[str, list[str]]] = []
_state: dict[str, str] = {"worker": "master"}


def worker_id(config: Any) -> str:
    """The xdist worker id (`gw0`, ...) of this process, or `master` without xdist.

    Read from the config pytest-xdist hands the worker, never from the environment alone: a
    child pytest started by a test inherits the environment but is not a worker (H-7).
    """
    if hasattr(config, "workerinput"):
        return str(config.workerinput.get("workerid") or "master")
    return "master"


def pytest_configure(config: Any) -> None:
    """Remember which process this is (the controller or an xdist worker)."""
    _state["worker"] = worker_id(config)


def per_worker(path: str, worker: str) -> str:
    """`path` itself without xdist, `path.<worker>` for a worker (node-id file, memory trace)."""
    return path if worker == "master" else f"{path}.{worker}"


def _fd_mode() -> str:
    """`1` (blocking), `report` (log only) or `` (off: unset or any other value)."""
    value = os.environ.get("NURAMESH_FD_CHECK", "")
    return value if value in ("1", "report") else ""


def annotation(module: str, leaked: list[str]) -> str:
    """One GitHub workflow-command line for a leaking module (escaped as the command needs)."""
    shown = ", ".join(leaked[:3]) + (", ..." if len(leaked) > 3 else "")
    text = f"{module} left {len(leaked)} database files open: {shown}"
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::warning title=Leaked DB handles::{text}"


def _summary_table() -> str:
    rows = ["| Module | Handles | Paths |", "| --- | --- | --- |"]
    total = 0
    for module, leaked in _findings:
        total += len(leaked)
        paths = "<br>".join(f"`{p}`" for p in leaked[:10]) + ("<br>..." if len(leaked) > 10 else "")
        rows.append(f"| {module} | {len(leaked)} | {paths.replace('|', '\\|')} |")
    head = "### Leaked database handles (report mode)\n\n"
    return (
        head + "\n".join(rows) + f"\n\n**Total: {total} handle(s) in {len(_findings)} module(s)**\n"
    )


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """Report mode: print the annotations and the totals, and append the table to the summary."""
    if _fd_mode() != "report" or not _findings:
        return
    for module, leaked in _findings:
        terminalreporter.write_line(annotation(module, leaked))
    total = sum(len(leaked) for _, leaked in _findings)
    terminalreporter.write_line(
        f"open database files (report): {len(_findings)} module(s), {total} handle(s)"
    )
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        try:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write(_summary_table())
        except OSError:
            pass


@pytest.fixture(scope="module", autouse=True)
def _open_handle_check(request: pytest.FixtureRequest) -> Iterator[None]:
    """Check a module for open database files (NURAMESH_FD_CHECK: `1` fails, `report` logs)."""
    mode = _fd_mode()
    if not mode:
        yield
        return
    gc.collect()
    before = open_database_files()
    yield
    gc.collect()
    after = open_database_files()
    remaining = list(after)
    for path in before:
        if path in remaining:
            remaining.remove(path)
    module = str(request.node.nodeid).removesuffix(".py")
    if mode == "report":
        if remaining:
            _findings.append((module, remaining))
        return
    message = judge(module, remaining)
    if message:
        pytest.fail(message, pytrace=False)


def pytest_runtest_logstart(nodeid: str, location: object) -> None:
    """Tell the guard which test is starting (for its kill message)."""
    target = os.environ.get("NURAMESH_GUARD_NODEID_FILE")
    if target:
        try:
            Path(per_worker(target, _state["worker"])).write_text(nodeid, "utf-8")
        except OSError:
            pass


# ---- memory trace ------------------------------------------------------------------------------

_trace: dict[str, float] = {"last": 0.0}


def _rss_mb() -> float:
    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout.strip()
        return int(out) / 1024
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def _peak_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> None:
    """After the last test of a module, append its row to MEMTRACE_LOG."""
    target = os.environ.get("MEMTRACE_LOG")
    if not target:
        return
    if nextitem is not None and nextitem.path == item.path:
        return
    path = Path(per_worker(target, _state["worker"]))
    rss = _rss_mb()
    if not path.exists() or path.stat().st_size == 0:
        path.write_text("module,rss_mb,delta_mb,peak_mb\n", "utf-8")
        _trace["last"] = 0.0
    with path.open("a", encoding="utf-8") as handle:
        name = item.nodeid.split("::", 1)[0]
        handle.write(f"{name},{rss:.1f},{rss - _trace['last']:.1f},{_peak_mb():.1f}\n")
    _trace["last"] = rss


MEMTRACE_HEADER = "module,rss_mb,delta_mb,peak_mb"


def merge_memtrace(target: str) -> int:
    """Merge the per-worker traces `<target>.<id>` into `<target>` (header once, sorted by module).

    Returns the number of rows merged; 0 (and nothing touched) when there are no worker files.
    """
    parts = sorted(Path(target).parent.glob(Path(target).name + ".gw[0-9]*"))
    rows: list[str] = []
    for part in parts:
        try:
            rows += [line for line in part.read_text("utf-8").splitlines() if line][1:]
        except OSError:
            continue
    if not parts:
        return 0
    rows.sort(key=lambda row: row.split(",", 1)[0])
    Path(target).write_text("\n".join([MEMTRACE_HEADER, *rows]) + "\n", "utf-8")
    for part in parts:
        part.unlink(missing_ok=True)
    return len(rows)


def pytest_sessionfinish(session: Any) -> None:
    """Worker: hand the open-handle findings to the controller. Controller: merge the traces."""
    config = session.config
    if hasattr(config, "workeroutput"):
        config.workeroutput["fd_findings"] = json.dumps(_findings)
        return
    if hasattr(config, "workerinput"):
        return
    target = os.environ.get("MEMTRACE_LOG")
    # Only the controller of an xdist run merges: in a plain run a stale `.gw0` left by a killed
    # earlier xdist run must not replace the rows just written.
    option = getattr(config, "option", None)
    if target and getattr(option, "numprocesses", None):
        merge_memtrace(target)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: object) -> None:
    """Controller (xdist only): collect the findings a finished worker reported."""
    if not hasattr(node, "workeroutput"):
        return
    raw = node.workeroutput.get("fd_findings")
    if not raw:
        return
    for module, leaked in json.loads(raw):
        _findings.append((module, list(leaked)))
    _findings.sort(key=lambda finding: finding[0])


# ---- write check -------------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parents[1]


_SNAPSHOT_SKIP = re.compile(
    r"(^|/)(\.claude|\.venv|venv|node_modules|__pycache__|\.git|\.mypy_cache|\.ruff_cache|\.pytest_cache|\.hypothesis)(/|$)"
)


def checkout_state(root: Path | None = None) -> dict[str, int]:
    """Every file in the checkout, ignored ones included -> its mtime in nanoseconds.

    The checkout is this repository, or the folder named by `NURAMESH_WRITE_CHECK_ROOT` (the
    tests of this check point it at a throwaway repository).
    """
    root = root or Path(os.environ.get("NURAMESH_WRITE_CHECK_ROOT") or _ROOT)
    names: set[str] = set()
    # tracked, untracked, and ignored files (a `*.tfplan` or `.coverage` is ignored, and writing
    # one is exactly the H-3 hazard); environments and caches are left out
    for flags in (
        ["ls-files"],
        ["ls-files", "-o", "--exclude-standard"],
        ["ls-files", "-o", "-i", "--exclude-standard"],
    ):
        out = subprocess.run(
            ["git", "-C", str(root), *flags, "-z"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        ).stdout
        names.update(name for name in out.split("\0") if name and not _SNAPSHOT_SKIP.search(name))
    state: dict[str, int] = {}
    for name in names:
        try:
            state[name] = (root / name).stat().st_mtime_ns
        except OSError:
            continue
    return state


def changed_files(before: Mapping[str, int], after: Mapping[str, int]) -> list[str]:
    """Files new, changed or removed between two `checkout_state` snapshots."""
    return sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )


@pytest.fixture(scope="module", autouse=True)
def _write_check(request: pytest.FixtureRequest) -> Iterator[None]:
    """With NURAMESH_WRITE_CHECK=1 a module must leave the checkout as it found it (H-5)."""
    if os.environ.get("NURAMESH_WRITE_CHECK") != "1":
        yield
        return
    before = checkout_state()
    yield
    touched = changed_files(before, checkout_state())
    if touched:
        shown = "\n  ".join(touched[:10])
        pytest.fail(
            f"{request.node.nodeid} wrote into the checkout ({len(touched)} file(s)):\n  {shown}\n"
            "Write under tmp_path; parallel workers share the checkout (under xdist the file may "
            "be another worker's, but it is still a write into the shared checkout).",
            pytrace=False,
        )
