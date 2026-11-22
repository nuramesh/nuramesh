"""Where NuraMesh keeps its state on disk. Stdlib only."""

from __future__ import annotations

import os
from pathlib import Path

DIR = ".nuramesh"


def state_path(root: Path, *parts: str) -> Path:
    """`<root>/.nuramesh/<parts>`: a team folder's state (the run database and so on)."""
    return root.joinpath(DIR, *parts)


def home_path(*parts: str) -> Path:
    """`~/.nuramesh/<parts>`: keys and sandbox copies that belong to the person, not to a team."""
    return Path(os.path.expanduser("~")).joinpath(DIR, *parts)
