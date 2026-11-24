"""`pytest.mark.serial`: a test that must not run at the same time as another serial test.

Under pytest-xdist (`-n N`) the tests of a run are spread over N worker processes. A test that
touches something one machine has once (a fixed port, one shared file, a global tool) is marked
`serial`, with a reason on the line above (checked by `scripts/check_timeout_overrides.py`):

    # serial: binds the fixed port the browser fixture expects
    @pytest.mark.serial
    def test_x(): ...

Two things keep serial tests apart, so it works with every `--dist` mode:

* the test takes an exclusive file lock while it runs (the file sits in the parent of the
  workers' base temp directory, shared by all workers of one xdist run and removed with pytest's
  own temp cleanup; plain runs have no other worker, the lock is free), so two serial tests never
  overlap even on different workers. The lock is a function-scoped fixture: module- or
  session-scoped fixtures of a serial test are set up outside it;
* it also gets the xdist group "serial" (used by `--dist loadgroup`: they all go to one worker);
  the conftest hook that adds it must be `@pytest.hookimpl(wrapper=True)` (marks before xdist
  reads the groups; `add_group_to_node_ids` runs after, see below).

Nothing changes for tests without the mark. Registered by name in `tests/conftest.py`.
"""

from __future__ import annotations

import fcntl
from collections.abc import Iterator
from pathlib import Path
from typing import IO

import pytest

GROUP = "serial"

# The hook every conftest needs. It is a wrapper: the mark is added before every other
# `pytest_collection_modifyitems` (xdist reads the groups in its own), and the node ids are
# fixed up after them (pytest 9.2 renamed `Node._nodeid` to `_id`, so xdist 3.8's
# `item._nodeid = id@group` no longer changes the id and the group would be ignored).
HOOK_SOURCE = (
    "@pytest.hookimpl(wrapper=True)\n"
    "def pytest_collection_modifyitems(config, items):\n"
    "    mark_serial_items(items)\n"
    "    result = yield\n"
    "    add_group_to_node_ids(config, items)\n"
    "    return result\n"
)


def lock_path(factory: pytest.TempPathFactory) -> Path:
    """This xdist run's lock file: workers' base temps share one parent, one run per parent."""
    return factory.getbasetemp().parent / "nuramesh-serial.lock"


def mark_serial_items(items: list[pytest.Item]) -> None:
    """Give every `serial` test the xdist group (for `--dist loadgroup`)."""
    for item in items:
        if item.get_closest_marker(GROUP) is not None:
            item.add_marker(pytest.mark.xdist_group(GROUP))


def add_group_to_node_ids(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Under pytest >= 9.2 give a worker's `serial` items the `@serial` id suffix themselves.

    pytest-xdist 3.8 (latest) sets `item._nodeid`, which pytest 9.2 no longer reads (it keeps a
    structured `_id`), so `--dist loadgroup` would see no group. Older pytest: nothing to do.
    """
    if not hasattr(config, "workerinput") or not config.getoption("loadgroup", False):
        return
    for item in items:
        if hasattr(item, "_id") and item.get_closest_marker(GROUP) is not None:
            if not item.nodeid.endswith(f"@{GROUP}"):
                node_id = type(item._id).parse(f"{item.nodeid}@{GROUP}")
                item._id = node_id


@pytest.fixture(autouse=True)
def _serial_lock(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Hold the run-wide exclusive lock for the whole of a `serial` test."""
    if request.node.get_closest_marker(GROUP) is None:
        yield
        return
    handle: IO[str] = lock_path(tmp_path_factory).open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()
