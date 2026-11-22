"""Set, rotate, revoke and read the stored key of one connection (R1.14c section 6 and 9).

The one place the API route and the CLI share: find the connection, decide first key or rotate,
require a reason, store the reason in the content store (only its hash reaches the ledger),
run the provider probe with exactly the new version, and make the connection update of a first
key in the activation transaction. `ModelKeys` (the service of PR 1) does the key work; nothing
here reveals a value except to hand it to the provider probe.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nuramesh.core.ledger import EventType
from nuramesh.core.model_catalog import ConnectionRecord, connection_key
from nuramesh.core.model_keys import DEFAULT_GRACE_SECONDS, StoredKey
from nuramesh.core.org import OPENAI_COMPATIBLE
from nuramesh.core.secret_store import SecretRef, SecretStore, SecretStoreError
from nuramesh.core.secrets_text import Redacted
from nuramesh.engine import anthropic as anthropic_adapter
from nuramesh.engine import openai_compatible as oac
from nuramesh.engine.connection_rules import connection_problem
from nuramesh.engine.model_keys import TEXTS, KeyChange, ModelKeys, Revoked
from nuramesh.engine.org_ledger import OrgLedger
from nuramesh.stores.base import EventDraft, Store, UnitOfWork

REASON_MIN, REASON_MAX = 3, 500
STORED_PROVIDERS = frozenset({"anthropic"})
NO_STORED_KEY_TEXT = (
    "stored keys are supported for anthropic only; "
    "openai-compatible keeps its configured credential"
)
TEST_CODES = frozenset(
    {"auth_failed", "not_found", "redirected", "server_error", "unreachable", "refused"}
)
"""The failure codes a connection test of a new version can answer; anything else is
`test_error`. An upstream text is never kept or returned."""
EXTRA_TEXTS = (
    "stored model keys are not allowed on a hosted deployment yet",
    "stored model keys on a hosted deployment need MFA, which is not available yet",
    "stored model keys on a hosted deployment need MFA enrollment",
    "this needs a fresh multi-factor step-up",
    "the connection changed while the key was being tested; nothing was activated",
)


class KeyOpRefused(Exception):  # noqa: N818 (a refusal, said in words)
    """A refusal with a fixed code and text that never quotes the request."""

    def __init__(self, status: int, code: str, text: str) -> None:
        self.status, self.code, self.text = status, code, text
        super().__init__(text)


def reason_ok(text: str | None) -> bool:
    return text is not None and REASON_MIN <= len(text.strip()) <= REASON_MAX


@dataclass(frozen=True)
class KeyView:
    key_id: str
    entry: StoredKey


def find_record(ledger: OrgLedger, provider: str, endpoint_id: str | None) -> ConnectionRecord:
    """The connection that takes the key, else a 404 (a foreign or unknown name answers alike)."""
    found = ledger.view().connections.get(provider, endpoint_id).record
    if found is None:
        raise KeyOpRefused(404, "not_found", f"no connection for provider '{provider}'")
    if found.provider not in STORED_PROVIDERS:
        raise KeyOpRefused(422, "invalid_request", NO_STORED_KEY_TEXT)
    return found


class KeyOps:
    """The key operations of one organization."""

    def __init__(
        self,
        store: Store,
        org_id: str,
        ledger: OrgLedger,
        content: Any,
        secrets: SecretStore,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store, self.org_id, self.ledger = store, org_id, ledger
        self.content, self.secrets = content, secrets
        self._clock = clock if clock is not None else store.now

    # -- helpers ---------------------------------------------------------------------------

    def record(self, provider: str, endpoint_id: str | None) -> ConnectionRecord:
        return find_record(self.ledger, provider, endpoint_id)

    def _keys(self, record: ConnectionRecord) -> ModelKeys:
        if self.content is None:
            raise KeyOpRefused(409, "org_key_unavailable", TEXTS["org_key_unavailable"])
        content, org, secrets_ = self.content, self.org_id, self.secrets

        def probe(ref: SecretRef) -> str | None:
            try:
                value = secrets_.get(ref)
            except SecretStoreError:
                return "test_error"
            if record.provider == OPENAI_COMPATIBLE:
                found = oac.probe(
                    record.endpoint, "key", None, get=oac.default_get, credential=lambda: value
                )
                code, ok = found.code, found.code in ("ok", "not_found")
            else:
                made = anthropic_adapter.probe(
                    anthropic_adapter.http_get, lambda: value, stored=True
                )
                code, ok = made.code, made.code == "ok"
            if ok:
                return None
            return code if code in TEST_CODES else "test_error"

        return ModelKeys(
            self.store,
            org,
            secrets_,
            lambda uow: content.ledger_hashing.bind(uow, org),
            test=probe,
            fixed_text=" ".join([*TEXTS.values(), *EXTRA_TEXTS]),
            clock=self._clock,
        )

    def _capture(self, uow: UnitOfWork, reason: str | None) -> str:
        return str(self.content.capture_reason(uow, self.org_id, (reason or "").strip()))

    # -- operations ------------------------------------------------------------------------

    def set_key(
        self,
        provider: str,
        endpoint_id: str | None,
        value: Redacted,
        *,
        by: str,
        reason: str | None,
        grace_seconds: int | None = None,
        via: str = "api",
        mfa: bool = False,
    ) -> tuple[KeyChange, str]:
        """Returns the change and the created_at of the new version."""
        record = self.record(provider, endpoint_id)
        name = connection_key(provider, endpoint_id)
        # `U_KEYHTTP` and the other use rules of a stored-key connection, before any probe or
        # repoint: a key is never sent over plain http, loopback included (review M1).
        problem = connection_problem(
            record.provider, record.endpoint, endpoint_id=record.endpoint_id, auth="stored"
        )
        if problem is not None:
            raise KeyOpRefused(422, "invalid_request", problem)
        keys = self._keys(record)
        ref = record.key_ref
        key = None if ref is None else keys.fold().key(ref)
        # After a revoke-all the next set continues the numbering on the same key id (spec 6, VK66).
        key_id = ref if key is not None and key.connection == name else None
        first = key_id is None or not key.current_version  # type: ignore[union-attr]
        if first and not reason_ok(reason):
            raise KeyOpRefused(422, "reason_required", "a reason is required to set a key")
        if not first and reason is not None and not reason_ok(reason):
            raise KeyOpRefused(422, "reason_required", "a reason must be 3 to 500 characters")

        def repoint(uow: UnitOfWork, new_key: str) -> None:
            # The record above was read before the provider probe (a network call). Re-read it
            # inside the activation transaction: if any connection entry was written meanwhile
            # (an edit, a removal, or another first key), nothing is activated (this rolls the
            # whole transaction back, so no `key_activated` is left without its repoint).
            now_record = self.ledger.view(uow).connections.get(provider, endpoint_id).record
            if now_record is None or now_record.seq != record.seq:
                raise KeyOpRefused(
                    409,
                    "connection_changed",
                    "the connection changed while the key was being tested; nothing was activated",
                )
            facts = {k: v for k, v in record.facts.items() if k != "key"}
            facts.update({"auth": "stored", "key_ref": new_key})
            uow.append(
                EventDraft(
                    stream=keys.stream,
                    type=EventType.MODEL_CONNECTION_UPDATED,
                    actor=by,
                    payload={
                        **facts,
                        "via": via,
                        "reason_hash": self._capture(uow, reason),
                    },
                )
            )

        change = keys.set_key(
            name,
            value,
            by=by,
            key_id=key_id,
            grace_seconds=DEFAULT_GRACE_SECONDS if grace_seconds is None else grace_seconds,
            via=via,
            mfa=mfa,
            on_first_activation=repoint if first else None,
            reason_hash_for=(
                (lambda uow: self._capture(uow, reason))
                if not first and reason is not None
                else None
            ),
        )
        entry = keys.fold().key(change.key_id)
        stamp = ""
        if entry is not None and change.version in entry.versions:
            stamp = entry.versions[change.version].stored_at.isoformat()
        return change, stamp

    def revoke(
        self,
        provider: str,
        endpoint_id: str | None,
        *,
        by: str,
        reason: str | None,
        version: int | None = None,
        via: str = "api",
        mfa: bool = False,
    ) -> Revoked:
        record = self.record(provider, endpoint_id)
        if not reason_ok(reason):
            raise KeyOpRefused(422, "reason_required", "a reason is required to revoke a key")
        if record.key_ref is None:
            raise KeyOpRefused(404, "unknown_key", TEXTS["unknown_key"])
        keys = self._keys(record)
        # Only keep the reason once the key (and the version asked for) is known to exist.
        known = keys.fold().key(record.key_ref)
        if known is None or not known.versions:
            raise KeyOpRefused(404, "unknown_key", TEXTS["unknown_key"])
        if version is not None and version not in known.versions:
            raise KeyOpRefused(404, "unknown_version", TEXTS["unknown_version"])
        return keys.revoke(
            connection_key(provider, endpoint_id),
            record.key_ref,
            by=by,
            reason_hash_for=lambda uow: self._capture(uow, reason),
            version=version,
            via=via,
            mfa=mfa,
        )

    def status(self, provider: str, endpoint_id: str | None) -> KeyView:
        record = self.record(provider, endpoint_id)
        if record.key_ref is None:
            raise KeyOpRefused(404, "unknown_key", TEXTS["unknown_key"])
        fold = ModelKeys(self.store, self.org_id, self.secrets, lambda _u: None).fold()  # type: ignore[arg-type,return-value]
        entry = fold.key(record.key_ref)
        if entry is None:
            raise KeyOpRefused(404, "unknown_key", TEXTS["unknown_key"])
        return KeyView(record.key_ref, entry)

    def health(self) -> str:
        try:
            self.secrets.list_versions(self.org_id)
        except SecretStoreError:
            return "unavailable"
        return "ok"
