"""Ports for tests that start a real server (xdist H-1): the OS picks the port, never a formula.

`serve_on_free_port` binds port 0 and reads the port back, so two workers (or two programs) never
collide. `forbid_fixed_port_binds` is the standing guard: it fails any bind of an inet socket to a
nonzero port (any host) unless the test carries `real_server_fixed_port`, a marker nothing uses.
"""

import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import HTTPServer, ThreadingHTTPServer
from typing import Any

import pytest

HOST = "127.0.0.1"


class QuietHTTPServer(HTTPServer):
    """`HTTPServer` for the tests' fake servers: no log noise when a client hangs up early."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], BrokenPipeError | ConnectionResetError):
            return
        super().handle_error(request, client_address)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` with the same rule as `QuietHTTPServer`."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], BrokenPipeError | ConnectionResetError):
            return
        super().handle_error(request, client_address)


def _default_config(app: Any, port: int, log_level: str) -> Any:
    import uvicorn

    return uvicorn.Config(app, host=HOST, port=port, log_level=log_level)


@contextmanager
def serve_on_free_port(
    app: Any,
    *,
    log_level: str = "error",
    config: Callable[[Any, int], Any] | None = None,
    timeout: float = 10.0,
) -> Iterator[tuple[Any, int]]:
    """Run `app` on a real uvicorn server bound to an OS-assigned port; yield (server, port).

    `config(app, port)` may build the uvicorn config (it receives port 0). The server is stopped
    and its thread joined on exit.
    """
    import uvicorn

    cfg = config(app, 0) if config is not None else _default_config(app, 0, log_level)
    server = uvicorn.Server(cfg)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        assert server.started, "the test server did not start"
        sock: socket.socket = server.servers[0].sockets[0]
        yield server, int(sock.getsockname()[1])
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def forbid_fixed_port_binds(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail a bind of an inet socket to a nonzero port, on any host (0.0.0.0, ::, 127.0.0.2, ...).

    Port 0 is the OS choosing a free one. Not covered: a bind made while a module- or
    session-scoped fixture sets up (this fixture is function-scoped) and a server started by a
    subprocess; the source scan in tests/test_xdist_hazards.py is the net for those.
    """
    if request.node.get_closest_marker("real_server_fixed_port") is not None:
        return
    real_bind = socket.socket.bind

    def bind(self: socket.socket, address: Any) -> None:
        if (
            isinstance(address, tuple)
            and len(address) >= 2
            and isinstance(address[1], int)
            and address[1] != 0
        ):
            raise AssertionError(
                f"binding {address[0]}:{address[1]} picks a port by hand; bind port 0 "
                "(tests/net.py serve_on_free_port) so parallel workers cannot collide"
            )
        real_bind(self, address)

    monkeypatch.setattr(socket.socket, "bind", bind)


_LOOPBACK_LITERALS = ("127.0.0.1", "::1")


def _loopback_problem(address: Any) -> str | None:
    """Why a connect to `address` is refused, or None for a literal loopback tuple."""
    if isinstance(address, str | bytes | bytearray):
        return f"a unix socket address {address!r}"  # AF_UNIX: not the network, not allowed either
    if isinstance(address, tuple) and address and isinstance(address[0], str):
        if address[0] in _LOOPBACK_LITERALS:
            return None
        return f"the host {address[0]!r} (only the literal 127.0.0.1 or ::1; a name would resolve)"
    return f"the address {address!r}"


@pytest.fixture
def loopback_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """No network in a test (spec openai-compatible 15, X7): `socket.connect` and `connect_ex`
    raise `AssertionError` for anything but the literal `127.0.0.1` or `::1`. A host that is a
    name (even `localhost`) raises, because connecting would resolve it; an AF_UNIX path raises.
    DNS (`getaddrinfo`) is not covered: use `no_dns` of tests/proxy_trap.py as well."""

    def refuse(address: Any) -> None:
        problem = _loopback_problem(address)
        if problem is not None:
            raise AssertionError(f"a test reached outside the loopback address: {problem}")

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> None:
        refuse(address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        refuse(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
