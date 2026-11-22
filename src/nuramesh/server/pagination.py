"""Cursor pagination of the list routes (R1.43 C4, spec/cli-api-naming section 4.4, case H08).

A request may send `limit` (1 to 200) and `cursor`. A client that sends `NuraMesh-Version:
2026-12` gets `{"items": [...], "next_cursor": "..." | null}`; without the header the route keeps
its named key (`tokens`, `users`, ...) and answers with `Deprecation`, `Sunset` and a
`rel="deprecation"` link to the upgrade notes. Without the header and without `limit` or `cursor`
the whole list comes back as today, so a new default never cuts a page a client gets now.

A cursor is `c1.` + base64url of `{"r": <route>, "k": <id of the last row sent>}`. It holds no
scope, no organization and no data: every page is read again under the caller's own scope, and a
cursor of another route or one whose row is gone is 400 `cursor_invalid`."""

import base64
import binascii
import json
from collections.abc import Callable, Sequence
from typing import Annotated, Any, Final

from fastapi import Query, Request
from fastapi.responses import JSONResponse

from nuramesh.server.deprecations import shape_headers
from nuramesh.server.sessions import AuthProblem
from nuramesh.server.versions import CURRENT, HEADER

PREFIX: Final = "c1."
MAX_LIMIT: Final = 200
DEFAULT_LIMIT: Final = 50
_MAX_CURSOR: Final = 1024
US: Final = "\x1f"
NOTES_ANCHOR: Final = "list-routes-page-with-a-cursor-r143"

Limit = Annotated[int | None, Query(ge=1, le=MAX_LIMIT)]
Cursor = Annotated[str | None, Query()]


def _invalid() -> AuthProblem:
    return AuthProblem(400, "cursor_invalid", "the cursor is not one this route issued")


def encode_cursor(route: str, key: str) -> str:
    """The opaque cursor after the row `key` of `route`."""
    raw = json.dumps({"r": route, "k": key}, separators=(",", ":")).encode()
    return PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_cursor(cursor: str, route: str) -> str:
    """The row key inside `cursor`; 400 `cursor_invalid` if unreadable or of another route."""
    if len(cursor) > _MAX_CURSOR or not cursor.startswith(PREFIX):
        raise _invalid()
    text = cursor[len(PREFIX) :]
    try:
        data = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
    except (ValueError, binascii.Error, RecursionError):  # JSON, unicode errors are ValueErrors
        raise _invalid() from None
    if (
        not isinstance(data, dict)
        or set(data) != {"r", "k"}
        or data["r"] != route
        or not isinstance(data["k"], str)
    ):
        raise _invalid()
    return data["k"]


def position(scope: str, pos: str) -> str:
    """The key of a cursor for a route with a path parameter: the parameter and the position."""
    return scope + US + pos if scope else pos


def decode_position(cursor: str, route: str, scope: str = "") -> str:
    """The position inside `cursor`; 400 `cursor_invalid` if it was made for another route or
    (for a route with a path parameter) another value of the parameter."""
    key = decode_cursor(cursor, route)
    if not scope:
        return key
    head, sep, pos = key.partition(US)
    if not sep or head != scope:
        raise _invalid()
    return pos


def invalid() -> AuthProblem:
    """The `cursor_invalid` problem, for a position a route cannot read."""
    return _invalid()


MAX_SEQ: Final = 2**62
"""The upper bound of a sequence (the same as `server.app.MAX_SEQ`; a test pins them)."""


def seq_position(text: str) -> int:
    """A ledger sequence kept in a cursor; anything else or above `MAX_SEQ` is `cursor_invalid`."""
    if not text.isascii() or not text.isdigit() or len(text) > 19 or int(text) > MAX_SEQ:
        raise _invalid()
    return int(text)


def paged(request: Request) -> bool:
    """Whether the caller sent `limit` or `cursor` (a client that did not keeps today's answer)."""
    return "limit" in request.query_params or "cursor" in request.query_params


def refuse_old_parameters(cursor: str | None, **old: object) -> None:
    """`cursor` together with an old parameter (`after_seq`, `after`, `before`) is 400."""
    if cursor is not None and any(v is not None for v in old.values()):
        raise AuthProblem(
            400, "invalid_request", "send either cursor or " + ", ".join(sorted(old)) + ", not both"
        )


def new_shape(request: Request) -> bool:
    """Whether the caller asked for the 2026-12 shapes (the header is validated earlier)."""
    return request.headers.get(HEADER.decode()) == CURRENT


def respond(
    request: Request,
    *,
    items: list[Any],
    old_key: str | None,
    next_cursor: str | None,
    extra: dict[str, Any] | None = None,
    old_extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    renamed: bool = True,
    native: bool = False,
) -> JSONResponse:
    """One answer in the shape the request asked for. `old_key` None is a bare array today: it
    stays one (with Deprecation) unless the caller pages, which opts into the envelope.
    `extra` members are in both shapes, `old_extra` only in the old one. `renamed` False: the
    old shape already used `items`, so nothing is deprecated. `native`: the old shape already
    pages by its own members (`next_after_seq`, `next_before`, ...), so it gets `next_cursor`
    only when a `cursor` was sent."""
    new = new_shape(request)
    asked = "cursor" in request.query_params if native else paged(request)
    body: Any
    if new or (old_key is None and asked):
        body = {"items": items, "next_cursor": next_cursor, **(extra or {})}
        new = True
    elif old_key is None:
        body = items
    else:
        body = {old_key: items, **(extra or {}), **(old_extra or {})}
        if asked:
            body["next_cursor"] = next_cursor
    response = JSONResponse(body, headers=dict(headers or {}))
    if not new and renamed:
        for name, value in shape_headers(NOTES_ANCHOR):
            response.headers[name.decode()] = value.decode()
    return response


def paginate(
    request: Request,
    rows: Sequence[dict[str, Any]],
    *,
    route: str,
    old_key: str | None,
    limit: int | None,
    cursor: str | None,
    headers: dict[str, str] | None = None,
    default: int = DEFAULT_LIMIT,
    scope: str = "",
    key: Callable[[dict[str, Any]], str] | None = None,
    extra: dict[str, Any] | None = None,
    old_extra: dict[str, Any] | None = None,
    renamed: bool = True,
) -> JSONResponse:
    """One page of `rows` (already scoped to the caller and narrowed for an agent, in the route's
    order, each with a unique string key: `id` unless `key` says otherwise) in the shape the
    request asked for. The cursor names the last row sent; a row not in `rows` is
    `cursor_invalid`, so a hidden row is never a position."""
    of: Callable[[dict[str, Any]], str] = key or (lambda r: str(r["id"]))
    start = 0
    if cursor is not None:
        wanted = decode_position(cursor, route, scope)
        ids = [of(r) for r in rows]
        if wanted not in ids:
            raise _invalid()
        start = ids.index(wanted) + 1
    if not new_shape(request) and limit is None and cursor is None:
        page = list(rows)
        nxt = None
    else:
        size = limit or default
        page = list(rows[start : start + size])
        more = start + size < len(rows)
        nxt = encode_cursor(route, position(scope, of(page[-1]))) if more and page else None
    return respond(
        request, items=page, old_key=old_key, next_cursor=nxt, extra=extra, old_extra=old_extra,
        headers=headers, renamed=renamed,
    )  # fmt: skip
