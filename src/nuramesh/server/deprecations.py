"""Renamed routes of R1.43: the old path is served by the
same handler for one release and answers with `Deprecation` (RFC 9745), `Sunset` (RFC 8594) and
`Link: <new>; rel="successor-version"`. A new path carries none of the three.

The release date is a constant of the source (December release, an internal decision record); the
open core never reads the clock for it, so a response never changes between two runs."""

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any, Final
from urllib.parse import quote, unquote_to_bytes

from starlette.routing import Match

DEPRECATION_START: Final = datetime(2026, 12, 1, tzinfo=UTC)
"""When the old names were announced: the first day of release R."""

SUNSET: Final = datetime(2027, 3, 1, tzinfo=UTC)
"""The day after which the old paths may go: at least 90 days after the start (OD-8)."""

DEPRECATED_ROUTES: Final[dict[tuple[str, str], tuple[str, ...]]] = {
    ("GET", "/v1/org/halt"): ("/v1/org/status",),
    ("POST", "/v1/approvals/{approval_id}/decision"): (
        "/v1/approvals/{approval_id}/approve",
        "/v1/approvals/{approval_id}/reject",
    ),
    ("POST", "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision"): (
        "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decisions",
    ),
    ("POST", "/v1/models/catalog/edit"): ("/v1/models/catalog/entries/edit",),
    ("POST", "/v1/models/catalog/approve"): ("/v1/models/catalog/entries/approve",),
    ("POST", "/v1/models/catalog/pause"): ("/v1/models/catalog/entries/pause",),
    ("POST", "/v1/models/catalog/retire"): ("/v1/models/catalog/entries/retire",),
    ("POST", "/v1/models/catalog/remove"): ("/v1/models/catalog/entries/remove",),
    ("POST", "/v1/models/catalog/regions"): ("/v1/models/catalog/regions",),
    ("POST", "/v1/models/assignments"): ("/v1/models/assignments/{scope}/{subject}",),
}
"""(method, old path template) -> the successor path templates (A29, A59, A60, A77 to A83, A85)."""

UPGRADE_NOTES: Final[dict[tuple[str, str], str]] = {
    ("GET", "/v1/org/halt"): "renamed-route-org-halt-r143",
    ("POST", "/v1/approvals/{approval_id}/decision"): "renamed-route-approval-decision-r143",
    ("POST", "/v1/runs/{run_id}/tasks/{task_id}/gates/{gate_id}/decision"): (
        "renamed-route-gate-decision-r143"
    ),
    ("POST", "/v1/models/catalog/edit"): "renamed-routes-catalog-entry-actions-r143",
    ("POST", "/v1/models/catalog/approve"): "renamed-routes-catalog-entry-actions-r143",
    ("POST", "/v1/models/catalog/pause"): "renamed-routes-catalog-entry-actions-r143",
    ("POST", "/v1/models/catalog/retire"): "renamed-routes-catalog-entry-actions-r143",
    ("POST", "/v1/models/catalog/remove"): "renamed-routes-catalog-entry-actions-r143",
    ("POST", "/v1/models/catalog/regions"): "renamed-route-catalog-regions-r143",
    ("POST", "/v1/models/assignments"): "renamed-route-model-assignments-r143",
}
"""Old route -> the anchor of its section in docs/upgrade-notes.md (a test checks each exists)."""

_SUNSET_HTTP: Final = format_datetime(SUNSET, usegmt=True)
_DEPRECATION: Final = f"@{int(DEPRECATION_START.timestamp())}"


class _Keep(dict[str, str]):
    """A successor parameter the old path does not carry (A85: the body held scope and id) stays
    as the template text."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def headers_for(old: tuple[str, str], params: dict[str, Any]) -> list[tuple[bytes, bytes]]:
    """The three headers of a deprecated route, the successor links filled from the request."""
    # Path params are decoded and the middleware runs before auth: percent-encode every one so no
    # control character, quote, angle bracket or non-ASCII byte can reach a header value.
    safe = {k: quote(str(v), safe="", errors="surrogateescape") for k, v in params.items()}
    links = ", ".join(
        f'<{new.format_map(_Keep(safe))}>; rel="successor-version"'
        for new in DEPRECATED_ROUTES[old]
    )
    links += f', <docs/upgrade-notes.md#{UPGRADE_NOTES[old]}>; rel="deprecation"'
    return [
        (b"deprecation", _DEPRECATION.encode()),
        (b"sunset", _SUNSET_HTTP.encode()),
        (b"link", links.encode()),
    ]


def shape_headers(anchor: str) -> list[tuple[bytes, bytes]]:
    """The headers of a list route answered in its old shape (R1.43 C4): the same path, so no
    successor link; `Link` points at the upgrade-notes section."""
    return [
        (b"deprecation", _DEPRECATION.encode()),
        (b"sunset", _SUNSET_HTTP.encode()),
        (b"link", f'<docs/upgrade-notes.md#{anchor}>; rel="deprecation"'.encode()),
    ]


class DeprecationHeaders:
    """ASGI: adds the headers to every answer on an old path (errors included)."""

    def __init__(self, app: Any, routes: Callable[[], Iterable[Any]]) -> None:
        self.app, self.routes = app, routes

    def _old(self, scope: Any) -> tuple[tuple[str, str], dict[str, Any]] | None:
        method = scope["method"]
        raw = scope.get("raw_path")
        if raw:  # decode the wire path exactly once, whatever the server did to scope["path"]
            wire = unquote_to_bytes(raw.split(b"?", 1)[0])  # bytes, then utf-8: %C3%A9 is one "é"
            scope = {**scope, "path": wire.decode("utf-8", "surrogateescape")}
        for route in self.routes():
            match, child = route.matches(scope)
            if match == Match.FULL:
                key = (method, getattr(route, "path", ""))
                if key in DEPRECATED_ROUTES:
                    return key, dict(child.get("path_params", {}))
                return None
        return None

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        found = self._old(scope) if scope["type"] == "http" else None
        if found is None:
            await self.app(scope, receive, send)
            return
        added = headers_for(*found)

        async def wrapped(message: Any) -> None:
            if message["type"] == "http.response.start":
                names = {k for k, _ in added}
                kept = [h for h in message["headers"] if h[0].lower() not in names]
                merged = [(k, v) for k, v in added if k != b"link"]
                links = [v for k, v in message["headers"] if k.lower() == b"link"]
                links.append(dict(added)[b"link"])  # a handler's own Link stays; ours is appended
                message["headers"] = [*kept, *merged, (b"link", b", ".join(links))]
            await send(message)

        await self.app(scope, receive, wrapped)


def document_renames(schema: dict[str, Any]) -> dict[str, Any]:
    """Say "renamed to <new path>" in the description of every deprecated operation (spec
    section 8); the operation already carries `deprecated: true`."""
    for (method, template), news in DEPRECATED_ROUTES.items():
        operation = schema.get("paths", {}).get(template, {}).get(method.lower())
        if operation is None:
            continue
        note = "Renamed to " + " and ".join(f"`{new}`" for new in news) + "."
        text = operation.get("description") or ""
        if note not in text:
            operation["description"] = f"{text}\n\n{note}".strip()
    return schema
