"""The one deny check for the CLI, the engine starts, the decision route and the delivery passes.

Order (R1.37): a halt in force (an unreadable halt is a halt), then an erased
organization id, then a lost key. It only reads: no key, folder, ring file, ledger entry,
security-log row or content row is created by calling it. It raises nothing.

**Fail closed everywhere (owner, Q2): unsure = erased.** A tombstone that cannot be read, a
`ContentError` of any code other than the key refusals, and any other exception while asking
about erasure are `tombstone_unavailable`: no wake, no marker, the CLI exits 3.

Imports `core`, `stores` and the engine's own halt and content modules; nothing from the CLI, the
server or Temporal (a test enforces it).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from nuramesh.core.model_governance import HALT_UNREADABLE_TEXT, HALTED_TEXT
from nuramesh.engine.content import (
    TOMBSTONE_UNAVAILABLE,
    ContentError,
    KeyDestroyedError,
    OrgIdRetiredError,
    OrphanedContentError,
    tombstone_unavailable,
)
from nuramesh.engine.org_halt import HaltWatch
from nuramesh.stores.base import Store

__all__ = [
    "Denial",
    "DenialKind",
    "UnavailableContent",
    "deny_checks",
    "erased_denial",
]

DenialKind = Literal["halted", "halt_unreadable", "erased", "key_lost", "tombstone_unavailable"]
UNAVAILABLE_TEXT = "whether the organization was erased could not be read: failing closed"


@dataclass(frozen=True)
class Denial:
    """The first reason a start or a wake is refused. `text` is what the CLI prints; `error` is
    the exception the engine raises for `erased`, `key_lost` and `tombstone_unavailable`."""

    kind: DenialKind
    org_id: str
    text: str
    error: Exception | None = None
    halt_seq: int | None = None  # `halted` only: seq of the deciding read's org.halted

    @property
    def is_halt(self) -> bool:
        return self.kind in ("halted", "halt_unreadable")


class UnavailableContent:
    """Stands in for a content service that could not be built (not for a missing extra, which
    is `None`): every question about erasure is unsure, so it is `tombstone_unavailable`."""

    def retired(self, org_id: str) -> Any:
        raise RuntimeError("the content service could not be built")

    def require_key_present(self, org_id: str) -> None:
        raise RuntimeError("the content service could not be built")


def _unavailable(org_id: str) -> Denial:
    return Denial(
        "tombstone_unavailable", org_id, UNAVAILABLE_TEXT, tombstone_unavailable(UNAVAILABLE_TEXT)
    )


def erased_denial(store: Store, org_id: str, *, content: Any | None = None) -> Denial | None:
    """The erased step alone: `erased` for a store or ring tombstone, `tombstone_unavailable` for
    anything that cannot be read (every exception, a `ContentError` of any code included)."""
    try:
        if content is None:
            found = store.key_tombstone(org_id)
            if found is None:
                return None
            error: Exception = OrgIdRetiredError(
                org_id, found.destroyed_at, approximate=found.at_approximate
            )
        else:
            found = content.retired(org_id)
            if found is None:
                return None
            error = OrgIdRetiredError(org_id, found.destroyed_at, approximate=found.at_approximate)
    except Exception:
        return _unavailable(org_id)
    return Denial("erased", org_id, str(error), error)


def deny_checks(
    store: Store,
    org_id: str,
    *,
    content: Any | None = None,
    halt: HaltWatch | None = None,
    key_check: bool = True,
    hashing: Callable[[], Any] | None = None,
) -> Denial | None:
    """The first reason in order (halt, erased id, lost key), or None. `content` is the content
    service when one can be built (None: the store tombstone decides erasure and the key check is
    skipped unless `hashing` is given); `halt` shares an engine's cursor; `key_check=False` skips
    the lost-key check. `hashing` is a factory of the ledger hashing, built only after the halt and
    erased steps passed: it does the key check when there is no content service (PH-KEY R5b: the
    ledger hashes need the key without the `content` extra)."""
    try:
        state = (halt if halt is not None else HaltWatch(store, org_id)).state()
        if state.halted:
            return Denial("halted", org_id, HALTED_TEXT, halt_seq=state.seq)
    except Exception:
        return Denial("halt_unreadable", org_id, HALT_UNREADABLE_TEXT)
    erased = erased_denial(store, org_id, content=content)
    if erased is not None:
        return erased
    if (content is None and hashing is None) or not key_check:
        return None
    try:
        checker = content if content is not None else hashing()  # type: ignore[misc]
        checker.require_key_present(org_id)
    except (KeyDestroyedError, OrphanedContentError) as exc:
        kind: DenialKind = "key_lost"
        return Denial(kind, org_id, str(exc), exc)
    except ContentError as exc:
        if exc.code == TOMBSTONE_UNAVAILABLE:
            return Denial("tombstone_unavailable", org_id, str(exc), exc)
        return _unavailable(org_id)
    except Exception:
        return _unavailable(org_id)
    return None
