"""The hard per-test timeout of tests/temporal (used by tests/temporal/conftest.py).

pytest-timeout's default `signal` method raises inside the main thread; a Temporal test stuck in
asyncio's cancel or teardown swallowed it and the shard hung 34+ minutes (merge-queue run
41722987549, test_a_new_worker_over_the_same_store_carries_on). The `thread` method dumps every
stack and ends the process with os._exit, so the shard FAILS (an xdist worker "crashed") fast.

180 s: the slowest recorded Temporal test takes 83 s on CI (.github/test-durations.json), so
180 s is more than twice that, and well under the 300 s default of the other tests. A test with
its own `pytest.mark.timeout` (justified by a `# timeout:` comment, ND119) keeps it.
"""

from __future__ import annotations

import faulthandler
import os
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO, Any

import pytest

HARD_TIMEOUT_SECONDS = 180
DUMP_LEAD_SECONDS = 10
DUMP_SUFFIX = "hang-dump"


def mark_hard_timeout(items: Iterable[Any], root: Path) -> None:
    """Add `timeout(180, method="thread")` to every item under `root` without its own timeout."""
    root = root.resolve()
    for item in items:
        path = Path(str(item.path)).resolve()
        if root not in path.parents or item.get_closest_marker("timeout") is not None:
            continue
        # timeout: hard thread limit so a hang in asyncio cancel fails the shard fast
        item.add_marker(pytest.mark.timeout(HARD_TIMEOUT_SECONDS, method="thread"))


def dump_base() -> Path:
    """Where a worker's hang dump goes: next to the guard's worker-current files (CI sets
    NURAMESH_GUARD_NODEID_FILE to $RUNNER_TEMP/worker-current, whose `Last test per worker` step
    prints every `worker-current.*` file), else $RUNNER_TEMP, else the system temp directory."""
    node_file = os.environ.get("NURAMESH_GUARD_NODEID_FILE")
    if node_file:
        return Path(node_file)
    return Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "worker-current"


def dump_path(worker: str) -> Path:
    """`<worker-current>.<worker>.hang-dump`: the file the stack dump of a hung test goes to."""
    return dump_base().with_name(f"{dump_base().name}.{worker}.{DUMP_SUFFIX}")


def dump_lead(item: Any) -> float | None:
    """Seconds after which to dump the stacks: the item's own thread-timeout marker minus a
    lead, so the dump is written while the process is alive (the thread method then ends it with
    os._exit and its own stderr dump is lost with the worker). None: no thread timeout."""
    marker = item.get_closest_marker("timeout")
    if marker is None or marker.kwargs.get("method") != "thread" or not marker.args:
        return None
    seconds = float(marker.args[0])
    return seconds - min(DUMP_LEAD_SECONDS, seconds / 2)


class HangDump:
    """Arm `faulthandler.dump_traceback_later` per test into a per-worker file; cancel and remove
    the file when the test ends. A file that is left behind is the stack of a test that hung."""

    def __init__(self, worker: str) -> None:
        self.path = dump_path(worker)
        self._file: IO[str] | None = None

    def arm(self, item: Any) -> None:
        delay = dump_lead(item)
        if delay is None:
            return
        try:
            self._file = self.path.open("w", encoding="utf-8")
            self._file.write(f"stack dump of {item.nodeid} after {delay:.0f} s\n")
            self._file.flush()
            faulthandler.dump_traceback_later(delay, repeat=False, file=self._file, exit=False)
        except OSError:
            self._file = None

    def disarm(self) -> None:
        if self._file is None:
            return
        faulthandler.cancel_dump_traceback_later()
        self._file.close()
        self._file = None
        self.path.unlink(missing_ok=True)


_DUMPS: dict[str, HangDump] = {}


def _dump() -> HangDump:
    worker = os.environ.get("PYTEST_XDIST_WORKER", "master")
    return _DUMPS.setdefault(worker, HangDump(worker))


@pytest.hookimpl(tryfirst=True)
def hang_dump_setup(item: pytest.Item) -> None:
    """`pytest_runtest_setup` (imported under that name by a conftest): arm the stack dump.

    It must be armed HERE and not around the protocol: pytest's own faulthandler plugin calls
    `dump_traceback_later` (one timer per process) at the start of the protocol when
    `faulthandler_timeout` is set (it is, 300 s, in pyproject), which would replace ours and
    send the dump to stderr, lost with the worker that os._exit ends."""
    _dump().arm(item)


@pytest.hookimpl(wrapper=True)
def hang_dump_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[Any]:
    """`pytest_runtest_protocol` wrapper: disarm and remove the dump file when the test ends. A
    file left behind is the stack of a test that hung."""
    try:
        return (yield)
    finally:
        _dump().disarm()
