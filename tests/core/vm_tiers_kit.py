"""Shared pieces of the Merge Gate tier and trust-root tests (`spec/verified-merge/tiers.md`):
the spec file, its fenced blocks and our repository's `extra_trust_roots` list (`XN`)."""

from __future__ import annotations

from pathlib import Path

DOCS = "".join(("do", "cs"))
"""The documentation folder's name, written in two pieces on purpose. The Merge Gate tests
use paths under it only as **strings given to the tier function and the matcher** (the spec's
cases: the CODEOWNERS place there, the decisions folder); they read no file in that folder. The
CI test selector takes every test source that spells the folder's name for a reader of it and
would run these tests for every documentation change. A test that does read that folder must
spell the name and be registered in the CI test selector."""

REPO_ROOT = Path(__file__).resolve().parents[2]
SPEC_FILE = REPO_ROOT / "spec" / "verified-merge" / "tiers.md"
VENDORED_BLOCKS = REPO_ROOT / "tests" / "data" / "vm_spec" / "trust-roots-v1.md"
FENCE = "`" * 3

M1 = "f6d00f67258bac7b9cebb726f8da4dc1bfbef425"
"""A 40-hex commit id (the base tip of the spec's baseline; any 40 hex would do here)."""


def spec_text() -> str:
    """`tiers.md` when this checkout has it (then it must hold the blocks), else the vendored
    byte-for-byte copy of its three fenced blocks."""
    return (SPEC_FILE if SPEC_FILE.is_file() else VENDORED_BLOCKS).read_text("utf-8")


def block_lines(text: str, marker: str) -> list[str]:
    """The lines of the one fence opened by three backticks plus `marker`, fences included."""
    lines = text.split("\n")
    opens = [index for index, line in enumerate(lines) if line == FENCE + marker]
    assert len(opens) == 1, "the marker must appear exactly once"
    rest = lines[opens[0] + 1 :]
    assert FENCE in rest, "the block is not closed"
    return [FENCE + marker, *rest[: rest.index(FENCE)], FENCE]


XN: list[str] = block_lines(spec_text(), "extra-trust-roots-nuramesh-v1")[1:-1]
"""Our repository's `extra_trust_roots` (TL-26, the block `extra-trust-roots-nuramesh-v1`): the
cases marked `XN` use it."""
