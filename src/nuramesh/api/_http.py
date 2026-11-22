"""The HTTP side of the public Python API (private module): the agent credential from the
environment, the server address, and one `httpx` session that sends every request with
`Authorization: Bearer nma_...` and `NuraMesh-Version`.

The credential comes **only** from `NURAMESH_AGENT_CREDENTIAL` or the file named by
`NURAMESH_AGENT_CREDENTIAL_FILE` (never a parameter, so no code path can hand the library a
session or a person's token), and only an agent credential (`nma_`) is accepted: a person's
token (`nmt_`) or a cookie value is refused at `bind()` time. Plain `http://` is accepted for
loopback hosts only. `httpx` is the `client` extra; importing it is deferred to the first binding.
"""

from __future__ import annotations

import asyncio
import os
import threading
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit

from nuramesh.core.auth import AGENT_CREDENTIAL_PREFIX, MAX_AGENT_CREDENTIAL_LENGTH
from nuramesh.errors import CredentialError, NuraMeshError

API_VERSION: Final = "2026-12"
"""The `NuraMesh-Version` the client speaks; equals `nuramesh.server.versions.CURRENT` (tested)."""
VERSION_HEADER: Final = "NuraMesh-Version"
CREDENTIAL_ENV: Final = "NURAMESH_AGENT_CREDENTIAL"
CREDENTIAL_FILE_ENV: Final = "NURAMESH_AGENT_CREDENTIAL_FILE"
URL_ENV: Final = "NURAMESH_URL"
CLIENT_EXTRA: Final = 'pip install "nuramesh[client]"'
LOOPBACK_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_TIMEOUT: Final = 10.0


def load_credential(env: Mapping[str, str] | None = None) -> str:
    """The agent credential from the environment; `CredentialError` (never quoting the value)
    when there is none, two sources, an unreadable file or a value that is not an agent's."""
    env = os.environ if env is None else env
    direct = env.get(CREDENTIAL_ENV)
    path = env.get(CREDENTIAL_FILE_ENV)
    if direct and path:
        raise CredentialError(
            f"both {CREDENTIAL_ENV} and {CREDENTIAL_FILE_ENV} are set; set exactly one"
        )
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                direct = handle.read().strip()
        except OSError as exc:
            raise CredentialError(
                f"{CREDENTIAL_FILE_ENV} names a file that cannot be read ({exc.strerror})"
            ) from None
    if not direct:
        raise CredentialError(
            f"no agent credential: set {CREDENTIAL_ENV} (or {CREDENTIAL_FILE_ENV}) to the "
            "credential an owner issued for this agent in the app"
        )
    value = direct.strip()
    shaped = (
        value.startswith(AGENT_CREDENTIAL_PREFIX)
        and len(AGENT_CREDENTIAL_PREFIX) < len(value) <= MAX_AGENT_CREDENTIAL_LENGTH
        and not any(c.isspace() for c in value)
    )
    if not shaped:
        raise CredentialError(
            f"{CREDENTIAL_ENV} is not an agent credential (they start with "
            f"{AGENT_CREDENTIAL_PREFIX!r}); a person's token or session is never accepted here: "
            "people decide in the app or through the API (OD-11)"
        )
    return value


def server_url(server: str | None, env: Mapping[str, str] | None = None) -> str:
    """The server's base URL: `server` or `NURAMESH_URL`; `https://`, or `http://` to a loopback
    host only (a credential never travels in clear to a remote host)."""
    env = os.environ if env is None else env
    raw = (server if server is not None else env.get(URL_ENV, "")).strip()
    if not raw:
        raise CredentialError(f"no server: pass server=... or set {URL_ENV}")
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or parts.query or parts.fragment:
        raise CredentialError("the server must be an https:// (or loopback http://) URL")
    if parts.username is not None or parts.password is not None:
        raise CredentialError("the server URL must not carry a user name or password")
    if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise CredentialError(
            "the server must be https:// (plain http:// is accepted for localhost only)"
        )
    return raw.rstrip("/")


@dataclass(frozen=True)
class Request:
    """One call to the server."""

    method: str
    path: str
    json: Mapping[str, Any] | None = None
    params: Mapping[str, Any] | None = None
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class Answer:
    """What came back: `failed` when nothing usable did (connection, TLS, no JSON body)."""

    status: int = 0
    body: Any = None
    version: str | None = None
    retry_after: float | None = None
    failed: bool = False
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def code(self) -> str | None:
        """The problem's stable machine name (`code`, else `title`), if the body is a problem."""
        if isinstance(self.body, dict):
            value = self.body.get("code") or self.body.get("title")
            return value if isinstance(value, str) else None
        return None

    @property
    def spoke_nuramesh(self) -> bool:
        return self.version == API_VERSION


def _httpx() -> Any:
    try:
        import httpx
    except ImportError:  # pragma: no cover - depends on the installed extras
        raise NuraMeshError(f"httpx is not installed: {CLIENT_EXTRA}") from None
    return httpx


class HttpSession:
    """One sync client for the binding's life and one async client per running event loop
    (never `asyncio.run` inside the library). Every exception of the transport becomes a
    failed `Answer`; the caller treats it as a deny."""

    def __init__(self, server: str, credential: str, *, transport: Any = None) -> None:
        from nuramesh import __version__

        self._httpx = _httpx()
        self._server = server
        self._transport = transport
        self._headers = {
            "Authorization": f"Bearer {credential}",
            VERSION_HEADER: API_VERSION,
            "Accept": "application/json, application/problem+json",
            "User-Agent": f"nuramesh-python/{__version__}",
        }
        self._sync: Any = None
        self._async: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = (
            weakref.WeakKeyDictionary()
        )
        self._lock = threading.Lock()

    def _client(self) -> Any:
        with self._lock:
            if self._sync is None:
                self._sync = self._httpx.Client(
                    base_url=self._server,
                    headers=self._headers,
                    transport=self._transport,
                    follow_redirects=False,  # a redirect is a denial: the Bearer never follows it
                    trust_env=False,  # an HTTP_PROXY in the environment must not see the Bearer
                )
            return self._sync

    def _aclient(self) -> Any:
        loop = asyncio.get_running_loop()
        with self._lock:
            for old in [k for k in self._async if k.is_closed()]:
                del self._async[old]  # a finished `asyncio.run` leaves no client behind
            client = self._async.get(loop)
            if client is None:
                client = self._async[loop] = self._httpx.AsyncClient(
                    base_url=self._server,
                    headers=self._headers,
                    transport=self._transport,
                    follow_redirects=False,
                    trust_env=False,
                )
            return client

    def send(self, request: Request) -> Answer:
        try:
            response = self._client().request(
                request.method,
                request.path,
                json=request.json,
                params=request.params,
                timeout=request.timeout,
            )
        except Exception:  # noqa: BLE001 - any transport failure is a deny, never an allow
            return Answer(failed=True)
        return self._answer(response)

    async def asend(self, request: Request) -> Answer:
        try:
            response = await self._aclient().request(
                request.method,
                request.path,
                json=request.json,
                params=request.params,
                timeout=request.timeout,
            )
        except Exception:  # noqa: BLE001 - as above
            return Answer(failed=True)
        return self._answer(response)

    @staticmethod
    def _answer(response: Any) -> Answer:
        headers = {k.lower(): v for k, v in response.headers.items()}
        retry: float | None = None
        raw_retry = headers.get("retry-after")
        if raw_retry is not None:
            try:
                retry = max(0.0, float(raw_retry))
            except ValueError:
                retry = None
        try:
            body = response.json()
        except ValueError:
            return Answer(
                status=response.status_code,
                version=headers.get(VERSION_HEADER.lower()),
                retry_after=retry,
                failed=True,
                headers=headers,
            )
        return Answer(
            status=response.status_code,
            body=body,
            version=headers.get(VERSION_HEADER.lower()),
            retry_after=retry,
            headers=headers,
        )

    def close(self) -> None:
        with self._lock:
            client, self._sync = self._sync, None
        if client is not None:
            client.close()

    async def aclose(self) -> None:
        with self._lock:
            client = self._async.pop(asyncio.get_running_loop(), None)
        if client is not None:
            await client.aclose()
