"""Shared helper for the stored-key service tests."""

import contextlib
import threading
from collections.abc import Iterator
from typing import Any

from nuramesh.core.secret_store import FakeSecretStore
from nuramesh.stores import Store


def track_transactions(store: Store, fake: FakeSecretStore) -> None:
    """Make `fake.in_transaction` true while `store.transaction()` is open on this thread."""
    state = threading.local()
    real = store.transaction

    @contextlib.contextmanager
    def tracked() -> Iterator[Any]:
        state.open = getattr(state, "open", 0) + 1
        try:
            with real() as uow:
                yield uow
        finally:
            state.open -= 1

    store.transaction = tracked  # type: ignore[method-assign]
    fake.in_transaction = lambda: getattr(state, "open", 0) > 0
