"""Where the tests' temp files live, and cleaning up what a session made (ND339 amended).

Pure functions, so conftest stays small and tests/test_temp_policy.py can check them.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

SHM = "/dev/shm"
_OWN_NAME = re.compile(r"^(pytest-\d+|popen-gw\d+)$")
_SHORT_NAME = re.compile(r"^pt-\d+$")


def short_basetemp(env: Mapping[str, str], pid: int) -> Path | None:
    """A short pytest base dir `<TMPDIR>/pt-<pid>` when TMPDIR is set, else None.

    pytest's default `<TMPDIR>/pytest-of-<user>/pytest-<n>/popen-gwN/<test>` overruns the 104-byte
    AF_UNIX path limit when TMPDIR is already long (CI: /run/nuramesh-tmp/runner-10)."""
    tmp = env.get("TMPDIR")
    if not tmp:
        return None
    return Path(tmp) / f"pt-{pid}"


def memory_temp_root(env: Mapping[str, str], platform: str = sys.platform) -> str | None:
    """`/dev/shm` for the default Linux run; None when the caller chose a place.

    Off with NURAMESH_TEST_TMPFS=0 or when TMPDIR is set (the runguard and run-small set both:
    their per-run directory is removed with the run). CI and a bare local run set neither, so
    their behaviour is unchanged."""
    if (
        platform.startswith("linux")
        and env.get("NURAMESH_TEST_TMPFS") != "0"
        and not env.get("TMPDIR")
        and os.path.isdir(SHM)
        and os.access(SHM, os.W_OK | os.X_OK)
    ):
        return SHM
    return None


OWN_MARKER = "NURAMESH_OWN_BASETEMP"
DEFAULT_LAYOUT = "default"


def claim_basetemp(
    env: Mapping[str, str], named_by_caller: bool, pid: int
) -> tuple[str | None, str]:
    """What the controller session may later delete: (basetemp to set or None, marker value).

    The marker is "" when the caller named --basetemp (never ours), the path of a `pt-<pid>` dir
    that did not exist yet (we create it), or "default" when pytest picks its own numbered dir."""
    if named_by_caller:
        return None, ""
    short = short_basetemp(env, pid)
    if short is None:
        return None, DEFAULT_LAYOUT
    if short.exists() or short.is_symlink():
        return None, DEFAULT_LAYOUT  # a stale pt-<pid> is not ours: let pytest number its own
    return str(short), str(short)


def remove_own_basetemp(basetemp: Path | None, env: Mapping[str, str]) -> bool:
    """Delete this session's own pytest base temp dir, and nothing the caller named or that
    existed before. Ownership comes from `OWN_MARKER`, set by the controller's `claim_basetemp`
    (xdist workers inherit it): the exact `pt-<pid>` path we created, its `popen-gw<n>` children,
    or, with "default", pytest's `pytest-of-<user>/pytest-<n>` / `popen-gw<n>`. Then drop an empty
    parent we made. NURAMESH_KEEP_BASETEMP=1 keeps it."""
    marker = env.get(OWN_MARKER, "")
    if basetemp is None or not marker or env.get("NURAMESH_KEEP_BASETEMP") == "1":
        return False
    parent = basetemp.parent.name
    if marker == DEFAULT_LAYOUT:
        ours = (
            (_OWN_NAME.match(basetemp.name) and parent.startswith("pytest-of-"))
            or (basetemp.name.startswith("popen-gw") and parent.startswith("pytest-"))
        ) and not _SHORT_NAME.match(basetemp.name)
    else:
        own = Path(marker)
        ours = basetemp == own or (basetemp.name.startswith("popen-gw") and basetemp.parent == own)
    if not ours:
        return False
    if basetemp.is_symlink() or not basetemp.is_dir():
        return False
    shutil.rmtree(basetemp, ignore_errors=True)
    if marker != DEFAULT_LAYOUT and basetemp == Path(marker):
        return True
    try:
        if marker == DEFAULT_LAYOUT and parent.startswith("pytest-of-"):
            basetemp.parent.rmdir()  # only succeeds when no other session still has a dir there
        elif marker != DEFAULT_LAYOUT:
            basetemp.parent.rmdir()  # the pt-<pid> dir, once its last worker dir is gone
    except OSError:
        pass
    return True
