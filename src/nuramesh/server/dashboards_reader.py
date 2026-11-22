"""Reads the ledger streams of one organization for the dashboards (spec/r211 section 4).

Streams come one at a time, in ascending stream-id order, each as its entries up to the head seen
when the walk started. Reads use the public `Store` read API only; no write lock is held.
"""

from collections.abc import Iterator

from nuramesh.core.ledger import LedgerEvent
from nuramesh.server.views import read_all
from nuramesh.stores.base import BadTimeError, Store
from nuramesh.telemetry.dashboards import Window


class DamagedRun(Exception):
    """One of the organization's runs holds a stored time that is not real (ND238)."""

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self.run_id = stream.removeprefix("run:")


def org_streams(
    store: Store, org_id: str, window: Window, *, all_streams: bool
) -> Iterator[list[LedgerEvent]]:
    """The run streams of `org_id` that can matter, each read in full (matching lookups need the
    whole stream). A stream whose newest entry is before the window cannot add a window figure,
    so it is skipped, except for the approvals view, whose waiting figure is state at `as_of`."""
    for stream in store.damaged_heads():  # not in heads(); only its own organization is told
        if stream.startswith("run:") and store.run_owner(stream) == org_id:
            raise DamagedRun(stream)
    heads = store.heads()
    for stream in sorted(heads):
        if not stream.startswith("run:"):
            continue
        head = heads[stream]
        if not all_streams and head.at < window.start:
            continue
        if store.run_owner(stream) != org_id:  # another organization's run: a raw row, no time
            continue
        try:
            yield [e for e in read_all(store, stream) if e.seq <= head.seq]
        except BadTimeError:
            raise DamagedRun(stream) from None
