"""Resolve an agent's file path against the workspace root before the policy sees it (R1.30 m1).

Policies match the text of a `write_file` target, so one file must have one spelling. A team tool
that takes a path from an agent calls `WorkContext.authorize_write(root, path)`, which uses this."""

import os
from pathlib import Path

__all__ = ["WorkspacePathError", "workspace_target"]


class WorkspacePathError(ValueError):
    """A path that is not inside the workspace, or has no safe spelling."""


def workspace_target(root: Path, path: str) -> str:
    """The root-relative POSIX name of `path`, or `WorkspacePathError`. Relative paths are taken
    from `root`; an absolute path must already lie inside it. `..` segments, backslashes and
    control characters are refused, and the real path (symbolic links followed) must stay inside
    the real root. A link inside the workspace that points to another file inside it is
    followed, so the policy sees the file that would really change."""
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise WorkspacePathError("a file path is a non-empty string without backslashes")
    if ".." in path.split("/"):
        raise WorkspacePathError("a file path may not contain '..'")
    real_root = Path(os.path.realpath(root))
    candidate = Path(path) if path.startswith("/") else root / path
    real = Path(os.path.realpath(candidate))
    try:
        relative = real.relative_to(real_root)
    except ValueError:
        raise WorkspacePathError("the path is outside the workspace") from None
    if relative == Path("."):
        raise WorkspacePathError("the path is the workspace itself")
    return relative.as_posix()
