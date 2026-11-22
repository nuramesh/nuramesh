"""Stored model key routes (R1.14c section 9, PR 1): set or rotate, revoke, read the state, health.

`PUT /v1/models/connections/{provider}/key`, `DELETE` the same path, `GET` the same path and
`GET /v1/models/keys/health`. The service is `engine.model_keys.ModelKeys` (nothing is re-done
here); this file adds who may call, the hosted and MFA rules of spec 7a, the request and response
shapes, and the connection update that the first key makes in the same transaction.

Rules in force: writes are **owner only** (a session of a person holding the `owner` role; admin
and every token or agent is refused 403 `forbidden`); the order of checks is spec 7a (role, then
hosted and not yet allowed, then MFA unavailable, enrolment, step-up, and only then the body).
A response never holds the value, its fingerprint or a store path; a refusal is a fixed code and
text that never quotes the request; every answer is `Cache-Control: no-store`. The route does not
honor `Idempotency-Key` for the write (the body holds a secret); revoke is idempotent by design.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ConfigDict, Field

from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.secret_store import SecretStore
from nuramesh.core.secrets_text import Redacted
from nuramesh.engine.key_ops import KeyOpRefused, KeyOps, find_record
from nuramesh.engine.model_keys import KeyRefused
from nuramesh.env_bool import EnvBoolError, env_bool
from nuramesh.hosted import HostedConfigError, hosted_from_env
from nuramesh.server.auth import Body
from nuramesh.server.authz import Caller
from nuramesh.server.model_routes import NO_STORE, ModelServices, _problem
from nuramesh.server.openapi_text import ALREADY_200
from nuramesh.stores.base import Store

DPA_ENV = "NURAMESH_KEYS_DPA_PUBLISHED"


def _dpa_ok() -> bool:
    try:
        return dpa_published()
    except EnvBoolError:
        return False


def dpa_published(env: Mapping[str, str] | None = None) -> bool:
    """`NURAMESH_KEYS_DPA_PUBLISHED` on the shared grammar. A malformed value raises
    `EnvBoolError`; `serve` already refused to start on it, and the key gate treats it as not
    published (fail closed)."""
    return env_bool(DPA_ENV, env)


KEY_BODY_MAX = 1024  # bytes; a key request body (spec VK59)
STEP_UP_WINDOW = timedelta(minutes=10)
REASON_MAX = 500
HOSTED_TEXTS = {
    "not_allowed_yet": "stored model keys are not allowed on a hosted deployment yet",
    "mfa_unavailable": (
        "stored model keys on a hosted deployment need MFA, which is not available yet"
    ),
    "mfa_enrollment_required": "stored model keys on a hosted deployment need MFA enrollment",
    "mfa_required": "this needs a fresh multi-factor step-up",
}


ADVISE_ROTATE_DAYS = 90


class SetKeyBody(Body):
    """`value` is write-only: it is never in a response, an error or the ledger."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    value: str = Field(min_length=1, max_length=2000, json_schema_extra={"writeOnly": True})
    grace_seconds: int | None = Field(default=None, ge=0, le=7 * 24 * 3600)
    reason: str | None = Field(default=None, max_length=REASON_MAX)


class RevokeBody(Body):
    version: int | None = Field(default=None, ge=1, le=1_000_000)
    reason: str | None = Field(default=None, max_length=REASON_MAX)


def _hosted() -> bool:
    try:
        return hosted_from_env()
    except HostedConfigError:
        return True  # an unreadable flag fails closed


def install_keys(
    app: FastAPI,
    config: NuraMeshConfig | None,
    services: ModelServices | None,
    secret_store: SecretStore | None,
    caller: Callable[..., Caller],
    store: Store,
    now: Callable[[], datetime],
    *,
    mfa_available: Callable[[], bool],
    injected_store: bool = False,
) -> None:
    """Add the key routes (each has a row in `authz.ROUTE_RULES` and `routes.ROUTE_SCOPES`)."""

    @app.exception_handler(KeyRefused)
    async def _refused(_: Any, exc: Exception) -> JSONResponse:
        assert isinstance(exc, KeyRefused)
        return _problem(exc.status, exc.code, str(exc))

    @app.exception_handler(KeyOpRefused)
    async def _op_refused(_: Any, exc: Exception) -> JSONResponse:
        assert isinstance(exc, KeyOpRefused)
        return _problem(exc.status, exc.code, exc.text)

    def need() -> ModelServices:
        if services is None or config is None:
            return _no_org()
        services.ensure_seeded()
        return services

    def _no_org() -> ModelServices:
        from nuramesh.server.sessions import AuthProblem

        raise AuthProblem(409, "no_config", "this server was started without an organization")

    Svc = Annotated[ModelServices, Depends(need)]

    def writer(who: Annotated[Caller, Depends(caller)]) -> Caller:
        """The checks of spec 7a that come before the body, in order (the role first)."""
        from nuramesh.server.sessions import AuthProblem

        if who.kind != "session" or not who.human or who.role != "owner" or who.actor is None:
            raise AuthProblem(403, "forbidden", "only an owner may change a stored key")
        return who

    def gate(who: Caller, *, dpa: bool) -> bool:
        """Spec 7a checks 4 to 7 (check 4 is for set and rotate only: `dpa`). Returns True when
        the write passed a hosted step-up."""
        from nuramesh.server.sessions import AuthProblem

        org = who.org_id
        assert org is not None and who.actor is not None
        factor = store.get_mfa_factor(org, who.actor)
        enrolled = factor is not None and factor.active
        hosted = _hosted()
        if hosted:
            if dpa and not _dpa_ok() and not injected_store:
                raise AuthProblem(403, "not_allowed_yet", HOSTED_TEXTS["not_allowed_yet"])
            if not mfa_available():
                raise AuthProblem(403, "mfa_unavailable", HOSTED_TEXTS["mfa_unavailable"])
            if not enrolled:
                raise AuthProblem(
                    403, "mfa_enrollment_required", HOSTED_TEXTS["mfa_enrollment_required"]
                )
        if hosted or enrolled:
            session = (
                None if who.session_id is None else store.get_auth_session(org, who.session_id)
            )
            at = None if session is None else session.mfa_verified_at
            if at is None or now() - at > STEP_UP_WINDOW:
                raise AuthProblem(403, "mfa_required", HOSTED_TEXTS["mfa_required"])
        return hosted

    def small_body(request: Request) -> None:
        """Spec VK59: a key request body is at most 1024 bytes (413 `too_large`). The guard has
        already required a plain Content-Length."""
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > KEY_BODY_MAX:
            from nuramesh.server.sessions import AuthProblem

            raise AuthProblem(413, "too_large", f"key requests are limited to {KEY_BODY_MAX} bytes")

    def set_gate(who: Annotated[Caller, Depends(writer)]) -> bool:
        return gate(who, dpa=True)

    def revoke_gate(who: Annotated[Caller, Depends(writer)]) -> bool:
        return gate(who, dpa=False)

    Passed = Annotated[bool, Depends(set_gate)]
    PassedRevoke = Annotated[bool, Depends(revoke_gate)]
    By = Annotated[Caller, Depends(writer)]

    def ops(svc: ModelServices, provider: str, endpoint_id: str | None) -> KeyOps:
        find_record(svc.ledger, provider, endpoint_id)  # unknown names answer 404 first
        if secret_store is None:
            raise KeyRefused("secret_store_unavailable")
        return KeyOps(svc.store, svc.config.org.id, svc.ledger, svc.content, secret_store)

    Query_ = Annotated[str | None, Query(max_length=100)]

    @app.put("/v1/models/connections/{provider}/key", status_code=201, responses=ALREADY_200)
    def put_key(
        provider: str,
        body: SetKeyBody,
        who: By,
        passed: Passed,
        svc: Svc,
        _size: Annotated[None, Depends(small_body)] = None,
        endpoint_id: Query_ = None,
    ) -> Response:
        """Set the first key of a connection, or rotate it (spec 6 steps 1 and 2)."""
        assert who.actor is not None
        change, created_at = ops(svc, provider, endpoint_id).set_key(
            provider,
            endpoint_id,
            Redacted(body.value),
            by=who.actor,
            reason=body.reason,
            grace_seconds=body.grace_seconds,
            mfa=passed,
        )
        if change.state == "failed":
            return _problem(
                422,
                "key_test_failed",
                "the connection test of the new key failed; the connection is unchanged",
                key_id=change.key_id,
                version=change.version,
                failure_code=change.failure_code,
            )
        out = {
            "key_id": change.key_id,
            "version": change.version,
            "state": change.state,
            "created_at": created_at,
        }
        return JSONResponse(out, status_code=201 if change.created else 200, headers=NO_STORE)

    @app.delete("/v1/models/connections/{provider}/key")
    def delete_key(
        provider: str,
        body: RevokeBody,
        who: By,
        passed: PassedRevoke,
        svc: Svc,
        _size: Annotated[None, Depends(small_body)] = None,
        endpoint_id: Query_ = None,
    ) -> Response:
        """Revoke one version or all of them (spec 6 step 3); a reason is required."""
        assert who.actor is not None
        done = ops(svc, provider, endpoint_id).revoke(
            provider,
            endpoint_id,
            by=who.actor,
            reason=body.reason,
            version=body.version,
            mfa=passed,
        )
        return JSONResponse(
            {"key_id": done.key_id, "revoked_versions": list(done.versions)}, headers=NO_STORE
        )

    @app.get("/v1/models/connections/{provider}/key")
    def get_key(
        provider: str,
        who: Annotated[Caller, Depends(caller)],
        svc: Svc,
        endpoint_id: Query_ = None,
    ) -> Response:
        """The versions and their states. The admin permission also sees the id and times; the
        read permission sees the states only. Never a value or a fingerprint."""
        view = KeyOps(
            svc.store, svc.config.org.id, svc.ledger, svc.content, secret_store or _Absent()
        ).status(provider, endpoint_id)
        at = now()
        admin = "admin" in {p.value for p in who.permissions}
        versions: list[dict[str, Any]] = []
        present = _listed(secret_store, svc.config.org.id)
        for number in sorted(view.entry.versions):
            ver = view.entry.versions[number]
            state: str = ver.state(at)
            if state == "current" and present is not None and (view.key_id, number) not in present:
                state = "key_missing"  # the ledger says current, the store lost it (VK28)
            item: dict[str, Any] = {"version": number, "state": state}
            if admin:
                item["created_at"] = ver.stored_at.isoformat()
                item["activated_at"] = (
                    None if ver.activated_at is None else ver.activated_at.isoformat()
                )
                item["grace_until"] = (
                    None if ver.grace_until is None else ver.grace_until.isoformat()
                )
            versions.append(item)
        out: dict[str, Any] = {"versions": versions}
        if admin:
            out["key_id"] = view.key_id
            for ver in view.entry.versions.values():
                if ver.state(at) == "current" and ver.activated_at is not None:
                    age = (at - ver.activated_at).days
                    out["age_days"] = age
                    out["rotate_advised"] = age >= ADVISE_ROTATE_DAYS  # advice, never a block
        return JSONResponse(out, headers=NO_STORE)

    @app.get("/v1/models/keys/health")
    def keys_health(svc: Svc) -> Response:
        """Whether the secret store answers: `{"store": "ok" | "unavailable"}` and nothing else."""
        if secret_store is None:
            return JSONResponse({"store": "unavailable"}, headers=NO_STORE)
        state = KeyOps(svc.store, svc.config.org.id, svc.ledger, svc.content, secret_store).health()
        return JSONResponse({"store": state}, headers=NO_STORE)


def _listed(secret_store: SecretStore | None, org_id: str) -> set[tuple[str, int]] | None:
    """What the store holds for the org (metadata only), or None when it cannot say (a store that
    is down is never reported as a lost key)."""
    if secret_store is None:
        return None
    try:
        return {(m.ref.key_id, m.ref.version) for m in secret_store.list_versions(org_id)}
    except Exception:  # noqa: BLE001 (any store failure means unknown)
        return None


class _Absent:
    """Stands in for a missing store in read-only fold use (any call fails closed)."""

    def __getattr__(self, name: str) -> Any:
        from nuramesh.core.secret_store import SecretStoreError

        raise SecretStoreError("unavailable")
