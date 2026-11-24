"""Command-line order of test arguments (#928).

pytest drops a folder's conftest fixtures for a file it meets again after files of another folder
(`tests/temporal/a.py tests/b.py tests/temporal/c.py`: `c.py` lost `fixture 'test_server'`). The
arguments are therefore grouped by the resolved folder of their file part (a directory argument
is its own folder), in order of first appearance, and each folder's arguments keep their own
order. The test selection is unchanged.
"""

import os


def group_args_by_folder(args: list[str]) -> list[str]:
    """Stable grouping of positional test arguments by folder."""
    folders: dict[str, list[str]] = {}
    for arg in args:
        path = os.path.realpath(arg.split("::", 1)[0])
        folder = path if os.path.isdir(path) else os.path.dirname(path)
        folders.setdefault(folder, []).append(arg)
    return [arg for group in folders.values() for arg in group]
