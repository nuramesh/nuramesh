"""Load a dev script (scripts/..., not a package) under a name no other test file can share.

Under pytest-xdist the set of test files a worker runs, and so the order in which they import,
differs from run to run. A bare `sys.path.insert(0, scripts)` plus `import check_coverage` leaves
the folder on `sys.path` for the rest of the process, and a second folder with a module of the same
name would shadow it. `import_script` loads the file by path, registers it under
`nuramesh_script_<importing test module>_<stem>`, and keeps the script's own folders on `sys.path`
only while the script runs its import statements (ND138, hazard H-10).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]

# Scripts (folder/module) that put their own folders on `sys.path` at import time and keep them
# there (a ratchet, like `guard_hooks.KNOWN_LEAKS`): `import_script` fails on any other script
# that leaves
# `sys.path` changed. There is no duplicate module name today, so these are latent (H-10).
KNOWN_PATH_LEAKS = {
    "app/gen_contract": "adds scripts/app (and, through generate(), tests/)",
    "app/models_world": "adds tests/ and tests/server",
    "app/scenarios": "adds its own folder for the sibling helpers",
    "ci/cost_report": "adds scripts/ci for the sibling helpers",
    "release/sbom_image": "adds scripts/release for the sibling sbom_uvlock",
    "scripts/decisions_split": "adds scripts/ for the sibling helpers",
    "scripts/handoff_index": "adds scripts/ for the sibling helpers",
}


@contextmanager
def path_prepended(*folders: Path) -> Iterator[None]:
    """Put folders first on `sys.path` for the block, then remove exactly what was added."""
    added = [str(folder) for folder in folders]
    sys.path[:0] = added
    try:
        yield
    finally:
        for entry in added:
            if entry in sys.path:
                sys.path.remove(entry)


def import_script(relative: str | Path, owner: str | None = None) -> ModuleType:
    """Execute the file `relative` (to the repository root, or absolute) as a fresh module."""
    path = Path(relative)
    if not path.is_absolute():
        path = ROOT / path
    path = path.resolve()
    who = owner or str(sys._getframe(1).f_globals.get("__name__", "tests"))
    name = "nuramesh_script_" + re.sub(r"\W+", "_", f"{who}_{path.parent.name}_{path.stem}")
    if path.suffix == ".py":
        spec = importlib.util.spec_from_file_location(name, path)
    else:  # a script with no .py suffix (an executable such as `gh-wrapper`)
        spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, str(path)))
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    folders = [path.parent]
    if path.parent != ROOT / "scripts" and (ROOT / "scripts") in path.parents:
        folders.append(ROOT / "scripts")  # scripts/ci and scripts/infra import siblings in scripts/
    sys.modules[name] = module
    before = list(sys.path)
    try:
        with path_prepended(*folders):
            spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    left = [entry for entry in sys.path if entry not in before]
    key = f"{path.parent.name}/{path.stem}"  # "app/gen_contract": the folder and the module
    if left and key not in KNOWN_PATH_LEAKS:
        sys.modules.pop(name, None)
        sys.path[:] = before
        raise AssertionError(
            f"{key} left sys.path changed ({left}); a later test could import the wrong module. "
            "Remove the sys.path change from the script, or list it in KNOWN_PATH_LEAKS."
        )
    return module
