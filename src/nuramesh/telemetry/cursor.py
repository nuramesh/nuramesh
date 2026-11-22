"""The cursor file: the exporter's only state (E3 spec section 4.2).

JSON `{"version": 1, "cursors": {"<stream>": <seq>}}`, written atomically (temp file, fsync,
rename) with mode 0600. It is derived, loss-tolerant state outside the ledger: a missing,
unreadable or corrupt file starts every stream at 0, a full replay that is safe because span ids
and contents come from the ledger alone.
"""

import json
import os
import tempfile
from pathlib import Path

VERSION = 1


class CursorFile:
    """Loads and saves the per-stream cursors."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, int]:
        """The saved cursors, or `{}` when the file is missing, unreadable or malformed."""
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return {}
        cursors = data.get("cursors")
        if not isinstance(cursors, dict):
            return {}
        return {
            stream: seq
            for stream, seq in cursors.items()
            if isinstance(stream, str)
            and isinstance(seq, int)
            and not isinstance(seq, bool)
            and seq >= 0
        }

    def save(self, cursors: dict[str, int]) -> None:
        """Write the cursors atomically; raises `OSError` when the disk refuses (the caller
        treats that as a failed cycle)."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        body = json.dumps({"version": VERSION, "cursors": cursors}, sort_keys=True)
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=".cursors-", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)
