"""Outbound requests never reach a private network by accident (R1.30 review 1, ND320 item 11).

Model connections are set by an owner, but a name or an address they set may point inside the
network (cloud metadata at 169.254.169.254, an internal service, the loopback of the server
itself) and the connection test answers with the status class. So every outbound connection made
through these handlers resolves the host **itself**, refuses the whole request when any answer is
private, loopback, link-local or otherwise not globally routable (IPv4 and IPv6, IPv4-mapped
included), and then connects to the address it checked (no second lookup that could answer
differently). `NURAMESH_ALLOW_PRIVATE_NETWORK=1` lifts the refusal for self-hosted setups with a
local model, but **only in a process that has recorded it**: the setting is honoured after
`record_allowance` has appended the ledger entry `network.private_allowed` in that process (the
API server at start, and every CLI process that seeds the catalog: `run`, `eval`, `worker`,
`models`, `calibrate`). A process that reads the variable and never recorded it keeps refusing, so
the setting can never be in force without its ledger entry. The open core sends no telemetry and
checks no licence: nothing here calls out on its own.

The handler classes are built when used, not at import: the Temporal workflow sandbox refuses a
module that subclasses an `urllib` or `http.client` class while it is imported."""

import socket
from collections.abc import Mapping
from typing import Any, Final

from nuramesh.core.ledger import EventType
from nuramesh.core.netaddr import AddressClass, classify_address
from nuramesh.env_bool import env_bool
from nuramesh.stores.base import EventDraft, Store

ENV: Final = "NURAMESH_ALLOW_PRIVATE_NETWORK"
REFUSED_TEXT: Final = (
    "refused: the host is a private, loopback or link-local address (or its name resolves to one); "
    f"set {ENV}=1 on the server to allow a model on your own network"
)


class PrivateNetworkError(OSError):
    """The request was refused before any connection: the host is on a private network."""

    def __init__(self) -> None:
        super().__init__(REFUSED_TEXT)


def allow_private_network(env: Mapping[str, str] | None = None) -> bool:
    """`NURAMESH_ALLOW_PRIVATE_NETWORK`: true to allow private hosts (default false). Anything but
    a clear yes or no is an error: `ValueError` naming the setting."""
    return env_bool(ENV, env)  # EnvBoolError is a ValueError naming the setting


_recorded = False  # set only by `record_allowance`: the entry is in the ledger for this process


def record_allowance(store: Store, org_id: str) -> None:
    """Append the ledger entry `network.private_allowed` (actor `system`, no address, no content)
    and arm the allowance for this process. Call it once per process start, in every process that
    reads the setting as true; if the append fails the exception propagates and nothing is armed."""
    global _recorded
    with store.transaction() as uow:
        uow.append(
            EventDraft(
                stream=f"org:{org_id}",
                type=EventType.NETWORK_PRIVATE_ALLOWED,
                actor="system",
                payload={"setting": ENV},
            )
        )
    _recorded = True


def record_allowance_from_env(
    store: Store, org_id: str, env: Mapping[str, str] | None = None
) -> bool:
    """`record_allowance` when the setting is true; True if the allowance is now in force. A
    malformed value raises `ValueError` (the allowance then stays off)."""
    if not allow_private_network(env):
        return False
    record_allowance(store, org_id)
    return True


def _allowed() -> bool:
    """The setting at call time AND recorded in this process; a malformed value denies."""
    try:
        return _recorded and allow_private_network()
    except ValueError:
        return False


def is_private_address(address: str) -> bool:
    """Is this IP address one an outbound request must not reach: anything but class `public` of the
    one classifier (`core.netaddr`, NG-1 to NG-4). One pair of `[` `]` is
    stripped first. Text that is not exactly one IP address (a zone id, a name) is treated as
    private. The classifier refuses everything the stdlib `is_global` check did, and more."""
    if address.startswith("[") and address.endswith("]"):
        address = address[1:-1]
    return classify_address(address) is not AddressClass.PUBLIC


def connect(address: tuple[str, int], timeout: float | None = None) -> socket.socket:
    """`socket.create_connection` that refuses private hosts: the host is resolved here, every
    answer is checked (one private answer refuses the request) and the connection goes to the
    checked address."""
    host, port = address
    if _allowed():
        return socket.create_connection((host, port), timeout)
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    checked = [str(info[4][0]) for info in infos]
    if not checked or any(is_private_address(a) for a in checked):
        raise PrivateNetworkError
    error: OSError | None = None
    for ip in checked:
        try:
            return socket.create_connection((ip, port), timeout)
        except OSError as exc:  # try the next checked address
            error = exc
    assert error is not None
    raise error


def guarded_handlers() -> tuple[Any, Any]:
    """An `HTTPHandler` and an `HTTPSHandler` for `urllib.request.build_opener` whose connections
    go through `connect`. TLS still checks the certificate against the original host name."""
    import http.client
    import urllib.request

    from nuramesh.engine import call_signal

    class GuardedHTTP(http.client.HTTPConnection):
        def connect(self) -> None:
            self.sock = connect((self.host, self.port), self.timeout)
            call_signal.on_connected(self)  # R20.8: connected, the request goes out next

    class GuardedHTTPS(http.client.HTTPSConnection):
        def connect(self) -> None:
            sock = connect((self.host, self.port), self.timeout)
            context = self._context  # type: ignore[attr-defined]
            self.sock = context.wrap_socket(sock, server_hostname=self.host)
            call_signal.on_connected(self)

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: Any) -> Any:
            return self.do_open(GuardedHTTP, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: Any) -> Any:
            return self.do_open(GuardedHTTPS, req, context=getattr(self, "_context", None))

    return HTTPHandler(), HTTPSHandler()


def tracked_handlers() -> tuple[Any, Any]:
    """An `HTTPHandler` and an `HTTPSHandler` for `urllib.request.build_opener` whose connections
    report to the current call's signal. Without a signal they behave as the standard ones."""

    import http.client
    import urllib.request

    from nuramesh.engine import call_signal

    class TrackedHTTP(http.client.HTTPConnection):
        def connect(self) -> None:
            super().connect()
            call_signal.on_connected(self)

    class TrackedHTTPS(http.client.HTTPSConnection):
        def connect(self) -> None:
            super().connect()
            call_signal.on_connected(self)

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: Any) -> Any:
            return self.do_open(TrackedHTTP, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: Any) -> Any:
            return self.do_open(TrackedHTTPS, req, context=getattr(self, "_context", None))

    return HTTPHandler(), HTTPSHandler()
