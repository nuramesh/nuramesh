"""The organization's model state, read from its ledger stream (L5, ND92).

The catalog, the assignments and the connections are the fold of the entries on `org:<org>`
(`core.model_catalog`). `OrgLedger` keeps that fold in memory together with the sequence it has read
up to and catches up from the store on every use, so a change made by another process (a second
server, the CLI) is seen at the next use and the audit trail can never disagree with the state.

Two ways to read: **committed** (no unit of work: reads what the store has committed, and keeps the
fold) and **in a unit of work** (a writer inside its transaction: the cached fold plus everything
after it that the transaction sees; the cache is not updated, because the transaction may still be
rolled back). A caller gets a private copy it may change to build a candidate state.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from nuramesh.core.ledger import LedgerEvent
from nuramesh.core.model_catalog import (
    CatalogFold,
    ConnectionsState,
    apply_catalog_event,
    apply_connection_event,
    new_fold,
)
from nuramesh.core.prices import PriceBook
from nuramesh.stores.base import Store, UnitOfWork

PAGE: Final = 1_000


def org_stream(org_id: str) -> str:
    """The organization's stream id, `org:<id>`."""
    return f"org:{org_id}"


@dataclass
class LedgerView:
    """A private copy of the organization's folded model state at one moment."""

    catalog: CatalogFold
    connections: ConnectionsState
    head: int
    """The sequence of the last org-stream entry this copy has read."""


class OrgLedger:
    """The incrementally folded model state of one organization."""

    def __init__(self, store: Store, org_id: str, base_prices: PriceBook | None = None) -> None:
        self.store, self.org_id = store, org_id
        self.stream = org_stream(org_id)
        self._lock = threading.Lock()
        self._base = base_prices
        self._catalog = new_fold(base_prices)
        self._connections = ConnectionsState()
        self._head = 0

    @staticmethod
    def _apply(catalog: CatalogFold, connections: ConnectionsState, event: LedgerEvent) -> None:
        apply_catalog_event(catalog, event)
        apply_connection_event(connections, event)

    def view(self, uow: UnitOfWork | None = None) -> LedgerView:
        """The state now (module docstring: committed, or inside `uow`).

        **No store read happens while `_lock` is held.** A writer holds the store's transaction and
        then asks for this view; a reader that held `_lock` while reading the store would be the
        other half of a lock cycle (the connection test caught it)."""
        if uow is not None:
            with self._lock:
                catalog, connections, head = (
                    self._catalog.clone(),
                    self._connections.clone(),
                    self._head,
                )
            head = self._advance(
                catalog, connections, head, lambda after: uow.events_after(self.stream, after, PAGE)
            )
            return LedgerView(catalog, connections, head)
        while True:
            with self._lock:
                head = self._head
            page = self._committed(head)
            with self._lock:
                if self._head == head:  # nobody advanced it meanwhile
                    for event in page:
                        self._apply(self._catalog, self._connections, event)
                        self._head = event.seq
                    if len(page) < PAGE:
                        return LedgerView(
                            self._catalog.clone(), self._connections.clone(), self._head
                        )

    def _committed(self, after: int) -> list[LedgerEvent]:
        return list(self.store.read_stream(self.stream, after_seq=after, limit=PAGE))

    def _advance(
        self,
        catalog: CatalogFold,
        connections: ConnectionsState,
        head: int,
        read: Callable[[int], list[LedgerEvent]],
    ) -> int:
        while True:
            page = read(head)
            for event in page:
                self._apply(catalog, connections, event)
                head = event.seq
            if len(page) < PAGE:
                return head

    def set_base_prices(self, base: PriceBook | None) -> None:
        """Change the configuration's price book the fold de-duplicates against (tests, reloads)
        and start the fold again."""
        with self._lock:
            self._base = base
            self._catalog = new_fold(base)
            self._connections = ConnectionsState()
            self._head = 0
