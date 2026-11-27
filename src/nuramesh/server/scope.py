"""Which organization a ledger stream belongs to, and who may see it.

**One helper for every route.** `OrgScope.visible(org, stream)` is the only place that decides
whether a caller of organization `org` may see a stream; the routes call it and answer the
*missing* answer when it says no, so a foreign run and a missing run are the same to the caller.

The organization of a stream is in its **first** entry (`run:<id>` streams: the `org` field of
`run.started`; `org:<X>` streams: `X`), because the ledger streams table has no organization
column. A decision reads at most that one entry. The first entry never changes, so the answer is
kept once the stream exists (never for a missing stream, which may be created later).
"""

import threading
from collections import OrderedDict
from collections.abc import Collection, Mapping
from typing import Any, Final

from nuramesh.core.ledger import EventType
from nuramesh.core.org import RESERVED_ORG_IDS
from nuramesh.stores.base import BadTimeError, Store

MAX_REMEMBERED: Final = 10_000
"""Streams whose owner ONE organization remembers (oldest forgotten first, within that
organization only); a forgotten one is re-read. There is no cap on the number of organizations:
a memory exists only for an organization present in the store, so the total is bounded by
(organizations in the store) x this (ND168)."""
MAX_NEW_LOOKUPS: Final = 5_000
"""Streams one request may look up for the first time (spec R9)."""
MAX_NEW_LOOKUPS_PER_TICK: Final = 500
"""Streams one live tick may look up for the first time (spec R9)."""

LOCAL_INSPECTOR: Final = "\x00local-inspector"
"""The caller of an app with no organization at all (authentication off, loopback only): the one
caller that sees every stream. A distinct value on purpose: a caller with no organization (None)
sees nothing, so a future public route cannot show everything by forgetting to set one."""

NOBODY: Final = ""
"""The owner of a stream with no organization (never equals a real organization id)."""


def is_org(org: str | None) -> bool:
    """A real organization id: not None, not `NOBODY`, not `LOCAL_INSPECTOR`. Only such a value
    may name an organization in the content store or the ledger."""
    return isinstance(org, str) and org not in (NOBODY, LOCAL_INSPECTOR)


class LookupBudget:
    """How many first-time stream lookups a request or a tick may still make."""

    def __init__(self, limit: int) -> None:
        self.left = limit
        self.exhausted = False

    def take(self) -> bool:
        if self.left <= 0:
            self.exhausted = True
            return False
        self.left -= 1
        return True


class OrgScope:
    """Decides, for one store, which organization owns a stream (thread-safe).

    **One memory per organization, and nothing shared (ND167, ND168).** What a caller has looked
    up is remembered for that caller's organization only, with a fixed size cap of its own
    (`remembered`), and an entry is only ever evicted by the same organization's later lookups.
    No cap spans organizations, so one organization's traffic never changes what another is told
    (`truncated` would otherwise show other tenants' reads). A memory is created only
    for an organization present in the store (it has an `org:` stream, or the app serves it:
    `orgs`), and for the local inspector; any other caller gets the normal answer and no memory."""

    def __init__(
        self,
        store: Store,
        *,
        remembered: int = MAX_REMEMBERED,
        orgs: Collection[str] = (),
    ) -> None:
        self.store = store
        self._remembered = remembered
        self._served = frozenset(orgs)
        self._owners: dict[str, OrderedDict[str, str]] = {}
        self._lock = threading.Lock()

    def _present(self, caller: str | None) -> bool:
        """Whether `caller` is an organization of this store: a fact of the store and the app,
        never of any caller's traffic (so it cannot carry one tenant's reads to another)."""
        if caller == LOCAL_INSPECTOR:  # the one caller of an app with no organization
            return True
        if caller is None or not is_org(caller):
            return False
        return caller in self._served or self.store.head(f"org:{caller}") is not None

    def memories(self) -> int:
        """How many organizations hold a memory (for tests and capacity checks)."""
        with self._lock:
            return len(self._owners)

    def owner(self, stream: str, caller: str | None = None) -> str | None:
        """The organization of `stream`, `NOBODY` if it has none, or None if it doesn't exist.

        Reads at most the stream's first entry, and only when `caller`'s organization has not
        already looked it up (the memory is per organization)."""
        with self._lock:
            memory = self._owners.get(caller) if caller is not None else None
            if memory is not None:
                known = memory.get(stream)
                if known is not None:  # a hit does not refresh its place (oldest first, FIFO)
                    return known
        if stream.startswith("org:"):
            # an `org:` stream's owner is in its name; it still has to exist
            try:
                exists = self.store.head(stream) is not None
            except BadTimeError:  # the stream exists; its stored time is damaged (ND233)
                exists = True
            found = (stream[4:] or NOBODY) if exists else None
            if found in RESERVED_ORG_IDS:  # a system stream (store startup) belongs to no tenant
                found = NOBODY if exists else None
        else:
            try:
                first = next(iter(self.store.read_stream(stream, after_seq=0, limit=1)), None)
            except BadTimeError:  # a damaged time: the owner is read from the raw row (ND238)
                org = self.store.run_owner(stream) if stream.startswith("run:") else None
                found = org if org else NOBODY
            else:
                found = (
                    None if first is None else self._run_owner(stream, first.type, first.payload)
                )
        if found is not None and caller is not None:
            with self._lock:
                memory = self._owners.get(caller)
            if memory is None and self._present(caller):
                with self._lock:
                    memory = self._owners.setdefault(caller, OrderedDict())
            if memory is not None:
                with self._lock:
                    memory[stream] = found
                    while len(memory) > self._remembered:
                        memory.popitem(last=False)
        return found

    @staticmethod
    def _run_owner(stream: str, kind: str, payload: Mapping[str, Any]) -> str:
        if not stream.startswith("run:") or kind != EventType.RUN_STARTED.value:
            return NOBODY
        org = payload.get("org")
        return org if isinstance(org, str) and org else NOBODY

    def visible(self, org: str | None, stream: str) -> bool:
        """May a caller of `org` see `stream`? `org` `LOCAL_INSPECTOR` (no
        organization configured): every existing stream; None: none. Anyone else: only their own."""
        owner = self.owner(stream, org)
        if owner is None:
            return False
        return org == LOCAL_INSPECTOR or (is_org(owner) and owner == org)

    def is_remembered(self, stream: str, caller: str | None = None) -> bool:
        with self._lock:
            return caller is not None and stream in self._owners.get(caller, ())

    def visible_within(self, org: str | None, stream: str, budget: LookupBudget) -> bool | None:
        """Like `visible`, but a first-time lookup costs `budget`; None = not decided (budget
        spent), the caller leaves the stream for later or marks the answer truncated."""
        if not self.is_remembered(stream, org) and not budget.take():
            return None
        return self.visible(org, stream)

    def filter_streams(
        self,
        org: str | None,
        streams: list[str],
        budget: LookupBudget,
        *,
        stop_after: int | None = None,
    ) -> tuple[list[str], bool]:
        """The visible ones among `streams` (in order) and whether the search was cut short:
        `budget` ran out before every stream was decided. With `stop_after`, the search ends
        once that many are found (not cut short)."""
        kept: list[str] = []
        for stream in streams:
            if stop_after is not None and len(kept) >= stop_after:
                break
            decided = self.visible_within(org, stream, budget)
            if decided is None:
                return kept, True
            if decided:
                kept.append(stream)
        return kept, False
