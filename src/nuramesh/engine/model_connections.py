"""Keyless model connections of one organization, kept in the ledger (ND80 Q6, ND92, spec 2.8).

A connection is the org admin's non-secret setting for a keyless provider: which Azure endpoint the
Entra token may go to, and whether it is active. There is no secret here to store or to leak.

**The ledger is the state.** `model.connection_created`, `_updated` and `_removed` on the
organization's own stream (`org:<org>`) are folded in order (`core.model_catalog`); the fold lives
in `OrgLedger`, which catches up from the store on every use. A change is judged inside its
transaction against everything committed.

**The service enforces the adapter rule itself** (`connection_problem`, before the transaction): a
foreign endpoint, or a provider that needs a stored key, is refused and nothing is appended. The
fold has no such rule: whatever is in the ledger is state, and the resolver judges whether it can be
used (a direct ledger write of a bad endpoint resolves to `endpoint_invalid`).

**A repoint pauses what it re-aims.** Changing the endpoint of a connection that has had one before
(also after a removal) changes where every approved model of that provider sends its data. It needs
a reason, and in the same transaction every approved entry of the provider is paused again
(`because: endpoint_changed`): each needs a fresh recorded approval (OWNER CONFIRM: data location,
ND45). A first connection, a status change and a removal pause nothing.

Lock order: the catalog service's lock, then this one, then the store.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from nuramesh.core.endpoints import normalise
from nuramesh.core.ledger import EventType
from nuramesh.core.model_catalog import (
    ConnectionRecord,
    connection_key,
    is_repoint,
    same_endpoint,
)
from nuramesh.core.models import ENDPOINT_NOT_ALLOWED, U_ENDPOINT_OTHER
from nuramesh.core.org import OPENAI_COMPATIBLE, OrgGraph
from nuramesh.engine.catalog_service import REASON_TEXT, CatalogRefused
from nuramesh.engine.connection_rules import (
    SUPPORTED_PROVIDERS,
    auth_of,
    connection_problem,
)
from nuramesh.engine.content import (
    ContentError,
    ContentService,
    KeyDestroyedError,
    OrphanedContentError,
)
from nuramesh.engine.openai_compatible import U_AUTH, endpoint_id_problem
from nuramesh.engine.org_ledger import OrgLedger, org_stream
from nuramesh.stores.base import EventDraft, Store

REPOINT_REASON = "a reason (3 to 500 characters) is required to repoint a connection"


class ConnectionRefused(Exception):  # noqa: N818 (a refusal, said in words)
    """A connection write was refused and appended nothing. `path` is the request field at fault
    (`$.endpoint`, `$.provider`, `$.reason`)."""

    def __init__(
        self, problem: str, path: str = "$.endpoint", code: str = "invalid_request"
    ) -> None:
        self.problem, self.path, self.code = problem, path, code
        super().__init__(problem)


STORED_KEY_HELD = (
    "this connection has a stored key; remove it from the app or the API (owner login)"
)


class StoredKeyHeld(ConnectionRefused):
    """A removal asked to refuse a connection that holds a stored key, and this one does (the
    CLI has no owner login, design decision 2026-11-08). Nothing was appended."""

    def __init__(self) -> None:
        super().__init__(STORED_KEY_HELD, "$.provider", "stored_key_held")


@dataclass(frozen=True)
class ConnectionResult:
    """What `put` did: `created`, `changed` or `already`, the connection now, the ledger entries
    appended (their types, in order) and the approved entries a repoint paused again."""

    result: str
    record: ConnectionRecord | None
    events: tuple[str, ...] = ()
    repaused: tuple[str, ...] = ()
    ledger_seq: int | None = None


class ModelConnections:
    """Read and change the connections of one organization."""

    def __init__(
        self,
        store: Store,
        org: OrgGraph,
        ledger: OrgLedger,
        *,
        content: ContentService | None = None,
        catalog_lock: Any = None,
    ) -> None:
        self.store, self.org, self.ledger, self.content = store, org, ledger, content
        self.stream = org_stream(org.id)
        # Lock order, everywhere: the catalog service's lock, then `_lock`, then the store.
        self._catalog_lock = catalog_lock if catalog_lock is not None else threading.Lock()
        self._lock = threading.Lock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the service locks in the global order (catalog lock, then this one) around a
        keyed write; the transaction is opened inside (R1.35, lock order)."""
        with self._catalog_lock, self._lock:
            yield

    def list(self) -> list[ConnectionRecord]:
        """The connections now (well-formed ones), by provider."""
        return self.ledger.view().connections.records()

    def get(self, provider: str, endpoint_id: str | None = None) -> ConnectionRecord | None:
        """One connection now, or None."""
        return self.ledger.view().connections.get(provider, endpoint_id).record

    def _reason(self, uow: Any, reason: str | None, *, required: bool) -> str | None:
        text = (reason or "").strip()
        if not text:
            if required:
                raise ConnectionRefused(REPOINT_REASON, "$.reason")
            return None
        if not 3 <= len(text) <= 500:
            raise ConnectionRefused(REPOINT_REASON if required else REASON_TEXT, "$.reason")
        if self.content is None:
            raise CatalogRefused(
                503,
                "content_unavailable",
                "a reason is stored in the content store, which isn't available: it isn't "
                "configured",
            )
        try:
            return self.content.capture_reason(uow, self.org.id, text)
        except (KeyDestroyedError, OrphanedContentError):
            raise  # the route answers 409 org_key_destroyed / org_key_missing (R1.37 section 8)
        except ContentError as exc:
            raise CatalogRefused(
                503,
                "content_unavailable",
                f"a reason is stored in the content store, which isn't available: {exc}",
            ) from None

    def put(
        self,
        provider: str,
        endpoint: str,
        *,
        by: str,
        status: str = "active",
        reason: str | None = None,
        via: str = "api",
        endpoint_id: str | None = None,
        auth: str | None = None,
        key: str | None = None,
    ) -> ConnectionResult:
        """Create, change or repoint a connection (module docstring)."""
        with self.locked(), self.store.transaction() as uow:
            result = self.put_in(
                uow,
                provider,
                endpoint,
                by=by,
                status=status,
                reason=reason,
                via=via,
                endpoint_id=endpoint_id,
                auth=auth,
                key=key,
            )
        return result

    def put_in(
        self,
        uow: Any,
        provider: str,
        endpoint: str,
        *,
        by: str,
        status: str = "active",
        reason: str | None = None,
        via: str = "api",
        endpoint_id: str | None = None,
        auth: str | None = None,
        key: str | None = None,
    ) -> ConnectionResult:
        """`put` inside the caller's unit of work, with `locked()` held (R1.35).

        The record is built from the values this call wrote, never read back."""
        oac_provider = provider == OPENAI_COMPATIBLE
        if not oac_provider:
            endpoint = endpoint.strip().lower()  # a host name has no case (spec section 2.8)
        # A connection that holds a stored key keeps it through an ordinary edit (R1.14c, B1):
        # a request that names no credential, names `stored` again, or goes to a provider whose
        # credential is fixed is not a repoint. Only the key service starts `stored`.
        held = self.ledger.view(uow).connections.get(provider, endpoint_id).record
        keep_ref: str | None = None
        if (
            held is not None
            and held.auth == "stored"
            and (auth in (None, "stored") or not oac_provider)
        ):
            if not same_endpoint(held.endpoint, endpoint, provider):
                # A stored key is bound to the host it was set for (N1): moving the connection
                # to another host is a design decision (revoke, then set a new key) or an explicit
                # repoint to another credential, never a silent carry-over of the key.
                raise ConnectionRefused(
                    "a connection with a stored key keeps its endpoint; "
                    "move it with a new key or another credential",
                    "$.endpoint",
                )
            keep_ref, auth = held.key_ref, "stored"
        if auth == "stored" and keep_ref is None:  # only the key service writes `stored`
            raise ConnectionRefused(U_AUTH, "$.endpoint")
        problem = connection_problem(
            provider, endpoint, endpoint_id=endpoint_id, auth=auth, key=key
        )
        if problem is not None:
            if problem == ENDPOINT_NOT_ALLOWED or problem == endpoint_id_problem(
                provider, endpoint_id
            ):
                if problem == ENDPOINT_NOT_ALLOWED:
                    raise ConnectionRefused(
                        U_ENDPOINT_OTHER, "$.endpoint_id", "endpoint_id_not_allowed"
                    )
                raise ConnectionRefused(problem, "$.endpoint_id", "invalid_request")
            path = "$.provider" if "stored key" in problem else "$.endpoint"
            raise ConnectionRefused(problem, path)
        if status not in ("active", "disabled"):
            raise ConnectionRefused("status is active or disabled", "$.status")
        if oac_provider:
            endpoint = normalise(endpoint)
        auth_value = "stored" if keep_ref is not None else auth_of(provider, auth)
        view = self.ledger.view(uow)
        now = view.connections.get(provider, endpoint_id)
        current = now.record
        if (
            current is not None
            and current.status == status
            and same_endpoint(current.endpoint, endpoint, provider)
            and current.auth == auth_value
            and current.key == key
            and current.key_ref == keep_ref
        ):
            return ConnectionResult("already", current)
        removed = current is None and now.invalid_seq is None and now.ever
        repoint = is_repoint(provider, now, endpoint, auth_value, key)
        if oac_provider:
            if repoint:
                status = "disabled"  # a later explicit enable is a separate event
            created = not now.ever
        else:
            created = not now.ever or (
                removed
                and now.last_endpoint is not None
                and same_endpoint(now.last_endpoint, endpoint)
            )
        reason_hash = self._reason(uow, reason, required=repoint)
        extra: dict[str, Any] = {"via": via}
        if reason_hash is not None:
            extra["reason_hash"] = reason_hash
        facts: dict[str, Any] = {
            "provider": provider,
            "auth": auth_value,
            "endpoint": endpoint,
            "status": status,
        }
        if endpoint_id is not None:
            facts["endpoint_id"] = endpoint_id
        if key is not None:
            facts["key"] = key
        if keep_ref is not None:
            facts["key_ref"] = keep_ref
        if repoint and now.last_endpoint is not None:
            extra["old_endpoint"] = now.last_endpoint
            extra["old_key"] = now.last_key
        kind = EventType.MODEL_CONNECTION_CREATED if created else EventType.MODEL_CONNECTION_UPDATED
        first = uow.append(
            EventDraft(stream=self.stream, type=kind, actor=by, payload={**facts, **extra})
        )
        events = [kind.value]
        paused: list[str] = []
        if repoint:
            for entry in view.catalog.entries.values():
                if (
                    entry.provider == provider
                    and entry.endpoint_id == endpoint_id
                    and entry.status == "approved"
                ):
                    uow.append(
                        EventDraft(
                            stream=self.stream,
                            type=EventType.MODEL_CATALOG_PAUSED,
                            actor=by,
                            payload={
                                "provider": entry.provider,
                                **({"endpoint_id": entry.endpoint_id} if endpoint_id else {}),
                                "model": entry.model,
                                "previous": "approved",
                                "because": "endpoint_changed",
                                **extra,
                            },
                        )
                    )
                    events.append(EventType.MODEL_CATALOG_PAUSED.value)
                    paused.append(entry.ref)

        record = ConnectionRecord(
            provider=provider,
            endpoint_id=endpoint_id,
            auth=auth_value,  # type: ignore[arg-type]
            key=key,
            key_ref=keep_ref,
            endpoint=endpoint,
            status=status,  # type: ignore[arg-type]
            set_by=by,
            set_at=first.at,
            seq=first.seq,
        )
        return ConnectionResult(
            "created" if created else "changed",
            record,
            tuple(events),
            tuple(paused),
            first.seq,
        )

    def remove(
        self,
        provider: str,
        *,
        by: str,
        reason: str | None = None,
        via: str = "api",
        endpoint_id: str | None = None,
        refuse_stored: bool = False,
    ) -> bool:
        """Remove a connection; False if there is none. `refuse_stored` raises `StoredKeyHeld`
        (nothing written) when it holds a stored key, decided inside this transaction."""
        with self.locked(), self.store.transaction() as uow:
            return self.remove_in(
                uow,
                provider,
                by=by,
                reason=reason,
                via=via,
                endpoint_id=endpoint_id,
                refuse_stored=refuse_stored,
            )

    def remove_in(
        self,
        uow: Any,
        provider: str,
        *,
        by: str,
        reason: str | None = None,
        via: str = "api",
        endpoint_id: str | None = None,
        refuse_stored: bool = False,
    ) -> bool:
        """`remove` inside the caller's unit of work, with `locked()` held (R1.35)."""
        if provider not in SUPPORTED_PROVIDERS:
            raise ConnectionRefused(
                "this provider needs a stored key, which is not available", "$.provider"
            )
        id_problem = endpoint_id_problem(provider, endpoint_id)
        if id_problem is not None:
            if id_problem == ENDPOINT_NOT_ALLOWED:
                raise ConnectionRefused(id_problem, "$.endpoint_id", "endpoint_id_not_allowed")
            raise ConnectionRefused(id_problem, "$.endpoint_id", "invalid_request")
        view = self.ledger.view(uow)
        now = view.connections.get(provider, endpoint_id)
        if now.record is None and now.invalid_seq is None:
            return False
        if refuse_stored and now.record is not None and now.record.auth == "stored":
            raise StoredKeyHeld
        reason_hash = self._reason(uow, reason, required=False)
        payload: dict[str, Any] = {"provider": provider, "via": via}
        if endpoint_id is not None:
            payload["endpoint_id"] = endpoint_id
        if reason_hash is not None:
            payload["reason_hash"] = reason_hash
        uow.append(
            EventDraft(
                stream=self.stream,
                type=EventType.MODEL_CONNECTION_REMOVED,
                actor=by,
                payload=payload,
            )
        )
        held = now.record
        if held is not None and held.auth == "stored" and held.key_ref is not None:
            # Design decision (R1.14c, 2026-11-08): removing a connection revokes its stored key in
            # the same transaction. Ids only; the sweeper destroys the value like any revoked key
            # (and retries while the secret store is down).
            from nuramesh.engine.key_sweeper import read_fold

            key = read_fold(self.store, self.org.id, uow).key(held.key_ref)
            if key is not None and any(not v.revoked for v in key.versions.values()):
                revoked: dict[str, Any] = {
                    "connection": connection_key(provider, endpoint_id),
                    "key_id": held.key_ref,
                    "version": "all",
                    "via": via,
                    "cause": "connection_removed",
                }
                if reason_hash is not None:
                    revoked["reason_hash"] = reason_hash
                uow.append(
                    EventDraft(
                        stream=self.stream,
                        type=EventType.MODEL_KEY_REVOKED,
                        actor=by,
                        payload=revoked,
                    )
                )

        return True
