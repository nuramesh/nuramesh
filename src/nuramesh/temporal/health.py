"""The worker's health endpoint (ND347 6.3): `GET /healthz` on a loopback-only port.

200 `{"engine":"temporal","connected":true}` while the worker is serving (its poll loop is
running: `connected` is false once the worker stops, which includes a fatal connection
loss), else 503.
Off unless `NURAMESH_WORKER_HEALTH_PORT` is set. It answers nothing else and reveals nothing but
those two fields; it binds to loopback only (a probe from the same host or a sidecar).
"""

import json
import threading
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any, Final

PORT_ENV: Final = "NURAMESH_WORKER_HEALTH_PORT"
LOOPBACK: Final = "127.0.0.1"


class HealthConfigError(ValueError):
    """`NURAMESH_WORKER_HEALTH_PORT` is not a port number (the worker exits 2)."""


class HealthBindError(RuntimeError):
    """The health port could not be bound (in use, or not allowed); the worker exits 2."""


def port_from_env(environ: Mapping[str, str]) -> int | None:
    """The health port, or None when unset or empty (off)."""
    text = environ.get(PORT_ENV, "").strip()
    if not text:
        return None
    try:
        port = int(text)
    except ValueError:
        raise HealthConfigError(f"{PORT_ENV} must be a port number") from None
    if not 1 <= port <= 65535:
        raise HealthConfigError(f"{PORT_ENV} must be between 1 and 65535")
    return port


class HealthServer:
    """A tiny HTTP server on its own thread. Use as a context manager."""

    def __init__(self, port: int, connected: Callable[[], bool], *, host: str = LOOPBACK) -> None:
        if host != LOOPBACK:
            raise ValueError("the worker health endpoint binds to loopback only")
        self.host = host

        def state() -> bool:
            return bool(connected())

        class Handler(BaseHTTPRequestHandler):
            server_version = "nuramesh-health"  # no Python or library version in the replies
            sys_version = ""

            def version_string(self) -> str:
                return self.server_version

            def do_GET(self) -> None:  # noqa: N802 (the http.server hook name)
                if self.path != "/healthz":
                    self.send_error(404)
                    return
                up = state()
                body = json.dumps({"engine": "temporal", "connected": up}).encode()
                self.send_response(200 if up else 503)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass  # no access log: nothing here is worth a line

        try:
            self._server = ThreadingHTTPServer((host, port), Handler)
        except OSError:
            raise HealthBindError(
                f"can't bind the health port {port} on {host} "
                f"(in use, or not allowed): check {PORT_ENV}"
            ) from None
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> "HealthServer":
        self._thread.start()
        return self

    def close(self) -> None:
        if self._thread.is_alive():  # shutdown() would wait forever on a server never started
            self._server.shutdown()
        self._server.server_close()
        if self._thread.ident is not None:  # join() raises on a thread that was never started
            self._thread.join(timeout=5)

    def __enter__(self) -> "HealthServer":
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
