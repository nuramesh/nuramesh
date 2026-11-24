"""The API token endpoints (R1.22 PR 3).

An **owner in a human session** creates, lists and revokes tokens (the `admin` rule). The secret
is in the creation answer only (`Cache-Control: no-store`); listing and revoking show the token's
id, name, actor, scopes and times, never the secret or its hash.
"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict
from datetime import timedelta
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from nuramesh.core.auth import TokenScope
from nuramesh.server.auth import Body, EmptyBody
from nuramesh.server.authz import Caller
from nuramesh.server.idempotency import IdemContext
from nuramesh.server.pagination import Cursor, Limit, paginate
from nuramesh.server.sessions import AuthProblem
from nuramesh.server.tokens import TokenService

_TOKEN_ID = re.compile(r"tok_[0-9a-f]{24}")


class RenameTokenBody(Body):
    """The new name of a token. The handler reads the raw body (so a malformed id answers 404
    before the shape is judged, R12) and checks the shape itself; this model only documents it."""

    name: str


class NewTokenBody(Body):
    """A new API token. `name` is a label for the program or purpose (`ci-nightly`), **not a
    person's name** and should not be personal data; the ledger never holds it (it records
    `service:<id>`).
    Reserved: `cli`, `system`, `api`, `engine` and anything starting `tok_`."""

    name: str = Field(min_length=1, max_length=63)
    scopes: list[Literal["read", "run"]] = Field(min_length=1, max_length=2)
    expires_in_days: int = Field(default=90, ge=1, le=365)


def install_tokens(
    app: FastAPI,
    service: TokenService | None,
    caller: Callable[..., Caller],
    idem_dep: Callable[..., IdemContext],
) -> None:
    """Add `GET` and `POST /v1/auth/tokens`, `POST /v1/auth/tokens/{token_id}/revoke`, and
    `PATCH` and `DELETE /v1/auth/tokens/{token_id}` (#634)."""

    def need() -> TokenService:
        if service is None:
            raise AuthProblem(409, "no_config", "this server was started without an organization")
        return service

    def owner(who: Annotated[Caller, Depends(caller)]) -> str:
        if not who.human or who.actor is None:  # the rule already says so; checked again
            raise AuthProblem(403, "human_required", "this needs a person signed in")
        return who.actor

    By = Annotated[str, Depends(owner)]
    Idem = Annotated[IdemContext, Depends(idem_dep)]

    @app.get("/v1/auth/tokens")
    def list_tokens(
        by: By, request: Request, limit: Limit = None, cursor: Cursor = None
    ) -> Response:
        views = [asdict(v) for v in need().list()]
        return paginate(
            request, views, route="GET /v1/auth/tokens", old_key="tokens",
            limit=limit, cursor=cursor, headers={"Cache-Control": "no-store"},
        )  # fmt: skip

    @app.post("/v1/auth/tokens", status_code=201)
    def create_token(body: NewTokenBody, by: By) -> Response:
        svc = need()
        issued = svc.create(
            body.name,
            {TokenScope(s) for s in body.scopes},
            created_by=by,
            lifetime=timedelta(days=body.expires_in_days),
        )
        return JSONResponse(
            {"token": issued.token, **asdict(svc.view(issued.record)), "shown_once": True},
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/v1/auth/tokens/{token_id}/revoke")
    def revoke_token(token_id: str, body: EmptyBody, by: By, idem: Idem) -> Response:
        svc = need()
        if not _TOKEN_ID.fullmatch(token_id):
            raise AuthProblem(404, "not_found", "no such token")
        headers = {"Cache-Control": "no-store"}
        if not idem.keyed:
            return JSONResponse(asdict(svc.revoke(token_id, by=by)), headers=headers)
        with idem.effect() as uow:
            view = svc.revoke_in(uow, token_id, by=by)
            # the answer names the token's creator: erasing that person deletes the row (ND60)
            person = view.created_by if view.created_by.startswith("human:") else None
            return idem.respond(uow, 200, asdict(view), subject=person, headers=headers)

    def checked_id(token_id: str) -> None:
        if not _TOKEN_ID.fullmatch(token_id):  # before the body is looked at (R12 step 2)
            raise AuthProblem(404, "not_found", "no such token")

    async def raw_body(request: Request) -> bytes:
        return await request.body()

    def parsed(raw: bytes) -> Any:
        """The body as JSON; a repeated key is refused, never last-wins (which name was meant?).
        Nothing of the input is quoted."""

        def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            if len({k for k, _ in pairs}) != len(pairs):
                raise ValueError("duplicate key")
            return dict(pairs)

        try:
            return json.loads(raw, object_pairs_hook=no_duplicates)
        except (ValueError, RecursionError):  # not JSON, not UTF-8, repeated key, absurd nesting
            raise AuthProblem(422, "invalid_request", "the request is invalid") from None

    @app.patch(
        "/v1/auth/tokens/{token_id}",
        openapi_extra={
            "requestBody": {
                "content": {"application/json": {"schema": RenameTokenBody.model_json_schema()}},
                "required": True,
            }
        },
    )
    def rename_token(
        token_id: str, raw: Annotated[bytes, Depends(raw_body)], by: By, idem: Idem
    ) -> Response:
        svc = need()
        checked_id(token_id)  # before the body is looked at (R12 step 2)
        payload = parsed(raw)
        if (
            not isinstance(payload, dict)
            or set(payload) != {"name"}
            or not isinstance(payload["name"], str)
        ):  # exactly the key `name`, a string: never a framework validation code (R12 step 3)
            raise AuthProblem(422, "invalid_request", "the request is invalid")
        name: str = payload["name"]
        headers = {"Cache-Control": "no-store"}
        if not idem.keyed:
            return JSONResponse(asdict(svc.rename(token_id, name, by=by)), headers=headers)
        with idem.effect() as uow:
            view = svc.rename_in(uow, token_id, name, by=by)
            return idem.respond(uow, 200, asdict(view), headers=headers)

    @app.delete("/v1/auth/tokens/{token_id}")
    def delete_token(token_id: str, by: By, idem: Idem) -> Response:
        svc = need()
        checked_id(token_id)
        headers = {"Cache-Control": "no-store"}
        if not idem.keyed:
            return JSONResponse(asdict(svc.delete(token_id, by=by)), headers=headers)
        with idem.effect() as uow:
            view = svc.delete_in(uow, token_id, by=by)
            return idem.respond(uow, 200, asdict(view), headers=headers)
