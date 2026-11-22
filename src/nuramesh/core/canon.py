"""Canon: the team's shared source of truth, read from Git at a pinned commit (PLAN F.1; T26).

A **canon source** names a repository, one **full 40-character commit id** and the path patterns to
read. Branch and tag names are refused: they move, and the point of a pin is that "which canon did
the agent use" has exactly one answer. The loaded **canon** is the commit plus the matching text
documents; its **canon hash** covers the commit and every document's hash, and the engine records it
in the ledger (`canon.loaded`) so a run's canon is provable.

This module is pure data and rules; reading Git is `engine.canon` (a subprocess, no shell).

Path patterns use the same globs as `write_file` policies (`*` stays inside one path segment, `**`
crosses `/`). Limits keep a canon small enough to put in a prompt: at most `MAX_DOCUMENTS`
documents, `MAX_DOCUMENT_BYTES` each and `MAX_TOTAL_BYTES` in all; anything over a limit is an
error, never a silent truncation.
"""

import re
import unicodedata
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from nuramesh.core.canonical import content_hash
from nuramesh.core.verifier import CanonDocument

MAX_DOCUMENTS = 200
MAX_DOCUMENT_BYTES = 200_000
MAX_TOTAL_BYTES = 2_000_000
MAX_NAME_BYTES = 100_000  # all document names together, so `canon.loaded` always fits the ledger
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

PathPattern = Annotated[str, StringConstraints(min_length=1, max_length=300)]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CanonSource(_Frozen):
    """Where the canon lives: a local repository path, a pinned commit and path patterns."""

    repository: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    paths: tuple[PathPattern, ...] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _safe_patterns(self) -> Self:
        for pattern in self.paths:
            parts = pattern.replace("\\", "/").split("/")
            if (
                pattern.startswith("/")
                or ".." in parts
                or any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in pattern)
            ):
                raise ValueError(
                    f"canon path pattern {pattern!r} must be relative, without '..' "
                    "or control characters"
                )
        return self


class CanonVerification(_Frozen):
    """What `load_canon` proved: the object format, the root tree and how many objects were
    re-hashed. `shallow` is informational, not part of the proof."""

    object_format: str
    tree: str
    trees: int
    blobs: int
    shallow: bool


class Canon(_Frozen):
    """The documents read from one commit, in path order. `verification` is set by `load_canon`
    and is not part of the canon hash."""

    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
    documents: tuple[CanonDocument, ...]
    verification: CanonVerification | None = None

    @property
    def canon_hash(self) -> str:
        """SHA-256 over the commit and every document's name and content hash."""
        return content_hash(
            {
                "commit": self.commit,
                "documents": [
                    {"name": d.name, "hash": content_hash(d.text)} for d in self.documents
                ],
            }
        )

    def ledger_payload(self) -> dict[str, object]:
        """What `canon.loaded` records: the commit, the hash and each document's name and hash
        (never the text), plus `verification` when the canon was verified."""
        payload: dict[str, object] = {
            "commit": self.commit,
            "canon_hash": self.canon_hash,
            "documents": [{"name": d.name, "hash": content_hash(d.text)} for d in self.documents],
        }
        if self.verification is not None:
            payload["verification"] = self.verification.model_dump()
        return payload


def is_full_commit_id(value: str) -> bool:
    """True for a 40-character lowercase hexadecimal commit id."""
    return _COMMIT.fullmatch(value) is not None
