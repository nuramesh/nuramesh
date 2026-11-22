"""Test a stored connection: can it be reached, does it accept the Entra login (spec section 11).

One bounded, body-less, keyless `GET` of the provider's model listing (`engine.azure_openai.probe`),
no model call and no spend. It changes no state: a failed test never disables a connection and a
pass is not required to enable one.

**Order (the first failing step wins; a refusal writes nothing and requests no token):** the
connection must exist; the organization must not be halted; the attempt is **reserved**
(`model.connection_test_reserved`, one store transaction with the limit check, so parallel requests
cannot exceed the limits); then the endpoint is checked, the token requested, the one request made
and the result recorded (`model.connection_tested`, with `reservation_seq`).

**Limits** are read from the ledger, so they hold across processes and across parallel requests: at
most one test per provider per 10 seconds and 20 per organization per rolling hour, counted from the
**reservations** (a test whose request is still in flight, or whose process died, still counts).
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from nuramesh.core.ledger import EventType
from nuramesh.core.model_catalog import connection_key
from nuramesh.core.org import OPENAI_COMPATIBLE
from nuramesh.engine import anthropic as key_file_adapter
from nuramesh.engine import openai_compatible as oac
from nuramesh.engine.azure_openai import (
    ProbeTransport,
    http_get,
    probe,
    probe_text,
)
from nuramesh.engine.connection_rules import auth_of, connection_problem
from nuramesh.engine.credentials import CredentialSource
from nuramesh.engine.openai_compatible import endpoint_id_problem
from nuramesh.engine.org_ledger import PAGE, OrgLedger, org_stream
from nuramesh.stores.base import EventDraft, Store, UnitOfWork

PROVIDER_WINDOW: Final = timedelta(seconds=10)
ORG_WINDOW: Final = timedelta(hours=1)
ORG_LIMIT: Final = 20
HALTED_TEXT = "the organization is halted: no connection is tested until a human lifts it"


OAC_CODE_TEXT = {
    "ok": "the endpoint answered",
    "unauthorized": "the endpoint refused the credentials",
    "redirect": "the endpoint answered with a redirect, which is never followed",
    "server_error": "the endpoint answered with a server error",
    "unreachable": "the endpoint could not be reached",
    "unexpected_status": "the endpoint answered with an unexpected status",
}


class TestRefused(Exception):  # noqa: N818 (a refusal, said in words)
    """The test was not made and nothing was written. `status` is the HTTP status."""

    __test__ = False  # (not a pytest class)

    def __init__(
        self, status: int, code: str, text: str, retry_after_seconds: int | None = None
    ) -> None:
        self.status, self.code, self.text = status, code, text
        self.retry_after_seconds = retry_after_seconds
        super().__init__(text)


@dataclass(frozen=True)
class TestResult:
    """A recorded test (a failed probe is still a result: it is a report)."""

    __test__ = False  # (not a pytest class)

    provider: str
    ok: bool
    code: str
    text: str
    latency_ms: int
    ledger_seq: int
    reservation_seq: int
    called: bool


class ConnectionTester:
    """Tests the connections of one organization (module docstring)."""

    def __init__(
        self,
        store: Store,
        org_id: str,
        ledger: OrgLedger,
        *,
        credentials: CredentialSource,
        halted: Callable[[], bool],
        transport: ProbeTransport | None = None,
        key_file_transport: ProbeTransport | None = None,
        oac_get: oac.Get | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store, self.ledger, self.stream = store, ledger, org_stream(org_id)
        self._credentials, self._halted = credentials, halted
        self._transport, self._clock = transport, clock
        # `transport` serves the keyless providers only; a key-file provider never uses it, so a
        # transport meant for one provider cannot carry another provider's key (ND225)
        self._key_file_transport = key_file_transport
        self._oac_get = oac_get
        self._lock = threading.Lock()
        self._cursor = 0
        self._reservations: deque[tuple[str, datetime]] = deque()

    def _recent(self, uow: UnitOfWork, now: datetime) -> list[tuple[str, datetime]]:
        """The reservations in the last hour, oldest first, read incrementally (under the lock)."""
        cursor = self._cursor
        while True:
            page = uow.events_after(self.stream, cursor, PAGE)
            for event in page:
                if event.type == EventType.MODEL_CONNECTION_TEST_RESERVED.value:
                    provider = event.payload.get("provider")
                    endpoint_id = event.payload.get("endpoint_id")
                    if isinstance(provider, str) and (
                        endpoint_id is None or isinstance(endpoint_id, str)
                    ):
                        self._reservations.append((connection_key(provider, endpoint_id), event.at))
                cursor = event.seq
            if len(page) < PAGE:
                break
        self._cursor = cursor
        horizon = now - ORG_WINDOW
        while self._reservations and self._reservations[0][1] <= horizon:
            self._reservations.popleft()
        return list(self._reservations)

    def test(
        self, provider: str, *, by: str, via: str = "api", endpoint_id: str | None = None
    ) -> TestResult:
        """Test the connection to `provider` (module docstring); `endpoint_id` names the
        connection of an `openai-compatible` provider."""
        id_problem = endpoint_id_problem(provider, endpoint_id)
        if id_problem is not None:
            raise TestRefused(422, "invalid_request", id_problem)
        name = connection_key(provider, endpoint_id)
        state = self.ledger.view().connections.get(provider, endpoint_id)
        if state.record is None and state.invalid_seq is None:
            raise TestRefused(404, "not_found", f"no connection for provider '{provider}'")
        try:
            halted = self._halted()
        except Exception:
            halted = True  # fail closed
        if halted:
            raise TestRefused(409, "org_halted", HALTED_TEXT)
        record = state.record
        seq = record.seq if record is not None else state.invalid_seq
        assert seq is not None
        with self._lock, self.store.transaction() as uow:
            now = uow.now()
            recent = self._recent(uow, now)
            mine = [at for p, at in recent if p == name]
            if mine and now - mine[-1] < PROVIDER_WINDOW:
                wait = math.ceil((PROVIDER_WINDOW - (now - mine[-1])).total_seconds())
                raise TestRefused(429, "rate_limited", _limit_text(wait), retry_after_seconds=wait)
            if len(recent) >= ORG_LIMIT:
                oldest = recent[0][1]
                wait = math.ceil((oldest + ORG_WINDOW - now).total_seconds())
                raise TestRefused(429, "rate_limited", _limit_text(wait), retry_after_seconds=wait)
            reserved = uow.append(
                EventDraft(
                    stream=self.stream,
                    type=EventType.MODEL_CONNECTION_TEST_RESERVED,
                    actor=by,
                    payload={
                        "provider": provider,
                        **({"endpoint_id": endpoint_id} if endpoint_id is not None else {}),
                        "connection_seq": seq,
                        "via": via,
                    },
                )
            )
            self._reservations.append((name, reserved.at))
            self._cursor = reserved.seq
        endpoint = record.endpoint if record is not None else None
        problem = (
            "its last change is not valid"
            if record is None
            else (
                "its credential type does not match the provider"
                if provider != OPENAI_COMPATIBLE
                and record.auth not in (auth_of(provider), "stored")
                else connection_problem(
                    provider,
                    record.endpoint,
                    endpoint_id=endpoint_id,
                    auth=record.auth,
                    key=record.key,
                )
            )
        )
        if problem is not None:
            code, status_class, latency, called = "endpoint_invalid", None, 0, False
            text = f"the connection cannot be tested: {problem}"
        elif provider == OPENAI_COMPATIBLE:
            assert record is not None
            made = oac.probe(
                record.endpoint,
                record.auth,
                record.key,
                get=self._oac_get or oac.default_get,
                clock=self._clock,
                credential=self._credentials.getter(record),
            )
            code, status_class, latency, called = made.code, None, made.latency_ms, made.called
            text = made.detail or f"openai-compatible: {OAC_CODE_TEXT.get(code, code)}"
        elif provider in key_file_adapter.KEY_FILE_PROVIDERS:
            assert record is not None
            stored = record.auth == "stored"
            if stored and key_file_adapter.hosted_problem() is not None:
                # a stored key is not used on a hosted server by the resolver either
                keyed = key_file_adapter.ProbeFound("credential_unavailable", None, 0, False)
            else:
                keyed = key_file_adapter.probe(
                    self._key_file_transport or key_file_adapter.http_get,
                    self._credentials.getter(record),
                    clock=self._clock,
                    stored=stored,
                )
            code, status_class = keyed.code, keyed.status_class
            latency, called = keyed.latency_ms, keyed.called
            text = key_file_adapter.probe_text(code, status_class)
        else:
            assert endpoint is not None and record is not None
            found = probe(
                endpoint,
                self._credentials.getter(record),
                transport=self._transport or http_get,
                clock=self._clock,
            )
            code, status_class = found.code, found.status_class
            latency, called = found.latency_ms, found.called
            text = probe_text(code, status_class)
        # a 404 from the model list means the service answers, the connection is usable (spec 9)
        ok = code == "ok" or (provider == OPENAI_COMPATIBLE and code == "not_found")
        with self.store.transaction() as uow:
            written = uow.append(
                EventDraft(
                    stream=self.stream,
                    type=EventType.MODEL_CONNECTION_TESTED,
                    actor=by,
                    payload={
                        "provider": provider,
                        **({"endpoint_id": endpoint_id} if endpoint_id is not None else {}),
                        "endpoint": endpoint,
                        "connection_seq": seq,
                        "reservation_seq": reserved.seq,
                        "ok": ok,
                        "code": code,
                        "status_class": status_class,
                        "latency_ms": latency,
                        "probe": "list_models",
                        "via": via,
                    },
                )
            )
        return TestResult(provider, ok, code, text, latency, written.seq, reserved.seq, called)


def _limit_text(wait: int) -> str:
    return f"connection test limit reached: try again in {wait} s"
