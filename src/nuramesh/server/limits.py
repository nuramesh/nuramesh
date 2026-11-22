"""Abuse limits for the unauthenticated auth endpoints (R1.22 PR 2, T66).

Three small pieces, all applied **before** any password hashing:

- `RateLimiter`: a sliding window per client (in memory, bounded number of clients), so one
  address cannot hammer login, setup or refresh;
- `HashGate`: a memory budget for concurrent password hashes (each costs its scrypt memory,
  about 32 MiB by default), taken without waiting, so a flood gets a quick 429;
- `client_address`: who the client is, proxy-aware only when the operator says the proxy is trusted.

Lockout counters are not here: they are stored (`core.auth.LockoutPolicy`) so a restart keeps them.
"""

import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from nuramesh.server.proxies import TrustedProxies, trusts_peer

MAX_TRACKED_CLIENTS = 10_000


class RateLimiter:
    """At most `limit` events per `window` seconds for each key; thread-safe and bounded: when
    more than `max_keys` clients are tracked the least recently seen one is forgotten."""

    def __init__(
        self,
        limit: int,
        window: float,
        clock: Callable[[], float],
        *,
        max_keys: int = MAX_TRACKED_CLIENTS,
    ) -> None:
        if limit < 1 or window <= 0 or max_keys < 1:
            raise ValueError("limit, window and max_keys must be positive")
        self.limit, self.window, self.max_keys = limit, window, max_keys
        self._clock = clock
        self._lock = threading.Lock()
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, key: str) -> float:
        """Count one event. Returns 0.0 if it is allowed, else the seconds until it would be."""
        now = self._clock()
        with self._lock:
            hits = self._hits.pop(key, None) or deque()
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            self._hits[key] = hits  # most recently seen goes last
            if len(hits) >= self.limit:
                return max(0.001, self.window - (now - hits[0]))
            hits.append(now)
            while len(self._hits) > self.max_keys:
                self._hits.popitem(last=False)
            return 0.0


class HashGate:
    """A memory budget for concurrent password hashes. A hash takes its scrypt memory
    (`ScryptParams.memory_bytes`) from the budget for as long as it runs; `slot(cost)` never
    waits: it yields False when the budget has no room (or `cost` alone exceeds it) and the
    caller answers 429 at once. With the default budget of four default hashes that is at most
    4 x 32 MiB, however heavy a stored hash's parameters are."""

    def __init__(self, budget_bytes: int) -> None:
        if budget_bytes < 1:
            raise ValueError("the hashing memory budget must be positive")
        self.budget = budget_bytes
        self._used = 0
        self._lock = threading.Lock()

    @property
    def used(self) -> int:
        """Bytes currently reserved."""
        with self._lock:
            return self._used

    @contextmanager
    def slot(self, cost: int) -> Iterator[bool]:
        """`with gate.slot(cost) as got:` holds `cost` bytes of the budget for the block."""
        with self._lock:
            got = 0 < cost <= self.budget - self._used
            if got:
                self._used += cost
        try:
            yield got
        finally:
            if got:
                with self._lock:
                    self._used -= cost


def _strip_port(address: str) -> str:
    address = address.strip()
    if address.startswith("["):  # [v6]:port or [v6]
        return address[1:].split("]", 1)[0]
    if address.count(":") == 1:  # v4:port
        return address.split(":", 1)[0]
    return address


def client_address(request: Any, *, trusted_proxy: TrustedProxies | bool) -> str:
    """The client's address. When the socket peer is a trusted proxy it is the **last** entry of
    all `X-Forwarded-For` lines joined (the one the proxy itself appended; earlier ones are
    client-supplied, and so is any entry that looks like a proxy address), without a port;
    otherwise the socket's peer, and the header is ignored."""
    peer = request.client.host if request.client is not None else None
    if trusts_peer(trusted_proxy, None if peer is None else str(peer)):
        lines = request.headers.getlist("x-forwarded-for")
        last = _strip_port(",".join(lines).rsplit(",", 1)[-1]) if lines else ""
        if last:
            return last[:64]
    client = request.client
    return str(client.host) if client is not None else "unknown"
