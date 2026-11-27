"""Reading the organization's halt and role-pause state (E19, ND106).

The state is never stored: it is folded from the `org:<id>` stream (`core.breakers.halt_state`,
`core.breakers.role_states`). `read_halt_state` reads the stream; `HaltWatch` keeps a cursor so the
engine's hot path costs one head query when nothing changed, and serves **both** the org halt and
the paused roles from one cursor and one code path, so a run's stop check and `call_model` can
never disagree and an unreadable stream fails closed for both. This module imports nothing from
`run_control`, which imports it.
"""

import threading
from collections.abc import Callable

from nuramesh.core.breakers import (
    HaltState,
    RoleState,
    counts_for_halt,
    counts_for_role,
    halt_state,
    role_states,
)
from nuramesh.core.ledger import LedgerEvent
from nuramesh.stores.base import Store

PAGE = 500


class OrgHalted(Exception):  # noqa: N818 (a state, said in words)
    """The organization is halted (or its halt state could not be read): no new work starts."""


class RolePaused(Exception):  # noqa: N818 (a state, said in words)
    """The calling agent's role is paused: none of its model calls or tasks start."""

    def __init__(self, role: str) -> None:
        super().__init__(f"role '{role}' is paused")
        self.role = role


def org_stream_id(org_id: str) -> str:
    """The organization's stream, `org:<id>`."""
    return f"org:{org_id}"


def read_org_events(store: Store, org_id: str) -> list[LedgerEvent]:
    """Every committed entry of the organization's stream, in order."""
    out: list[LedgerEvent] = []
    cursor = 0
    while True:
        batch = list(store.read_stream(org_stream_id(org_id), after_seq=cursor, limit=PAGE))
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        cursor = batch[-1].seq


def read_halt_state(store: Store, org_id: str) -> HaltState:
    """The halt state now. Raises whatever the store raises: callers fail closed. The same code
    path as `HaltWatch`, so a run's stop check and `call_model` can never disagree."""
    return HaltWatch(store, org_id).state()


class HaltWatch:
    """Thread-safe, incremental read of one organization's halt state and paused roles."""

    def __init__(self, store: Store, org_id: str) -> None:
        self._store, self._stream = store, org_stream_id(org_id)
        self._cursor = 0
        self._halt: LedgerEvent | None = None
        self._roles: dict[str, LedgerEvent] = {}  # per role: the latest honored entry
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        head = self._store.head(self._stream)
        while head is not None and head.seq > self._cursor:
            batch = list(self._store.read_stream(self._stream, after_seq=self._cursor, limit=PAGE))
            if not batch:
                break
            for event in batch:
                if counts_for_halt(event):
                    self._halt = event
                if counts_for_role(event):
                    role = event.payload.get("role")
                    if isinstance(role, str):
                        self._roles[role] = event
            self._cursor = batch[-1].seq

    def state(self) -> HaltState:
        """The halt state as of now (reads only entries appended since the last look)."""
        with self._lock:
            self._refresh()
            return halt_state([self._halt] if self._halt is not None else [])

    def roles(self) -> dict[str, RoleState]:
        """The state of every role the stream mentions, as of now."""
        with self._lock:
            self._refresh()
            return role_states(self._roles.values())

    def snapshot(self) -> tuple[HaltState, dict[str, RoleState]]:
        """Halt state and role states from one look."""
        with self._lock:
            self._refresh()
            return (
                halt_state([self._halt] if self._halt is not None else []),
                role_states(self._roles.values()),
            )

    def role_paused(self, role: str) -> bool:
        """Whether `role` is paused now."""
        state = self.roles().get(role)
        return state is not None and state.paused


def halt_predicate(store: Store, org_id: str) -> Callable[[], bool]:
    """`() -> halted?` over one incremental watch of the org stream (E19, ND112). It raises what
    the store raises when the stream cannot be read, so every caller fails closed."""
    watch = HaltWatch(store, org_id)
    return lambda: watch.state().halted


__all__ = [
    "HaltWatch",
    "halt_predicate",
    "OrgHalted",
    "RolePaused",
    "org_stream_id",
    "read_halt_state",
    "read_org_events",
]
