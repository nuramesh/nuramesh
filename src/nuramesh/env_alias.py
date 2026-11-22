"""Read a setting that was renamed: the new name wins, the old name still works for one release.

A setting's name is a promise (`docs/configuration-env.md`), so a rename keeps the old name as a
warning alias (ND421). The warning names the variables, never a value.
"""

import logging
from collections.abc import Mapping

_log = logging.getLogger("nuramesh.config")


def read_renamed(environ: Mapping[str, str], new: str, old: str) -> tuple[str, str] | None:
    """`(name, value)` of the setting that is set: `new` first, else `old` (with a warning).

    Returns `None` when neither is set. The name is the one that was read, so an error message
    names the variable the user actually wrote. Both set: `new` wins and the old one is ignored.
    """
    if new in environ:
        if old in environ:
            _log.warning("%s is ignored because %s is set", old, new)
        return new, environ[new]
    if old in environ:
        _log.warning("%s is deprecated: use %s (the old name works for one more release)", old, new)
        return old, environ[old]
    return None
