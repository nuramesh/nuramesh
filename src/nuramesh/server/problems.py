"""The one renderer of `application/problem+json` (RFC 9457; R1.43 B, spec section 7).

Every error of the API is built here: `type` and `title` as before (`title` stays the code for
one release, OD-6) plus the stable machine name `code`. Nothing else in the server builds a
problem body (a test fails if a module does).
"""

from collections.abc import Mapping
from typing import Any, Final

from starlette.responses import JSONResponse

PROBLEM: Final = "application/problem+json"


def problem_body(status: int, code: str, detail: str, **extra: Any) -> dict[str, Any]:
    """The JSON object of one problem; `extra` members are added but never replace the core."""
    return {
        **extra,
        "type": f"urn:nuramesh:{code}",
        "code": code,
        "title": code,
        "status": status,
        "detail": detail,
    }


def problem_response(
    status: int,
    code: str,
    detail: str,
    *,
    headers: Mapping[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    """A problem+json response with the given status, optional headers and extension members."""
    return JSONResponse(
        problem_body(status, code, detail, **extra),
        status_code=status,
        media_type=PROBLEM,
        headers=dict(headers) if headers else None,
    )


_PROBLEM_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "required": ["type", "code", "title", "status", "detail"],
    "properties": {
        "type": {"type": "string", "description": "urn:nuramesh:<code>"},
        "code": {"type": "string", "description": "the stable machine name of the error"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
    },
}
_VALIDATION_SCHEMA: Final[dict[str, Any]] = {
    **_PROBLEM_SCHEMA,
    "properties": {
        **_PROBLEM_SCHEMA["properties"],
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "where": {"type": "string"},
                    "path": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
    },
}
_OLD_PAGING: Final = ("after_seq", "after", "before")
_HTTP_METHODS: Final = ("get", "post", "put", "patch", "delete")


def _phrase(status: str) -> str:
    from http import HTTPStatus

    try:
        return HTTPStatus(int(status)).phrase
    except ValueError:
        return "Error"


def document_problems(schema: dict[str, Any]) -> dict[str, Any]:
    """Make every error answer of every `/v1` operation `application/problem+json` (H11).

    The statuses are the existing error answers plus the ones the raise-site walker found in the
    handler (`error_statuses.ERROR_STATUSES`, generated). A route with a `cursor` parameter also
    lists 400 (`cursor_invalid`) and marks the old paging parameters deprecated (R1.43 C4). A 422
    on `/v1/auth/*` is masked, so its schema has no `problems` member (H12)."""
    from .error_statuses import ERROR_503_CODES, ERROR_STATUSES

    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    schemas["Problem"] = _PROBLEM_SCHEMA
    schemas["ValidationProblem"] = _VALIDATION_SCHEMA
    for path, item in schema.get("paths", {}).items():
        if not path.startswith("/v1"):
            continue
        for method in _HTTP_METHODS:
            op = item.get(method)
            if op is None:
                continue
            responses = op.setdefault("responses", {})
            statuses = {s for s in responses if s[:1] in ("4", "5")}
            statuses |= {str(s) for s in ERROR_STATUSES.get(f"{method.upper()} {path}", [])}
            query = [p for p in op.get("parameters", []) if p.get("in") == "query"]
            if any(p.get("name") == "cursor" for p in query):
                # a cursor can be unreadable (400 `cursor_invalid`) and replaces the old parameters
                statuses.add("400")
                responses.setdefault("400", {"description": "cursor_invalid or invalid_request"})
                for p in query:
                    if p.get("name") in _OLD_PAGING:
                        p["deprecated"] = True
            for status in sorted(statuses):
                masked = path.startswith("/v1/auth/")
                name = "ValidationProblem" if status == "422" and not masked else "Problem"
                existing = responses.get(status, {})
                codes = (
                    ERROR_503_CODES.get(f"{method.upper()} {path}", []) if status == "503" else []
                )
                text = existing.get("description") or ", ".join(codes) or _phrase(status)
                responses[status] = {
                    "description": text,
                    "content": {PROBLEM: {"schema": {"$ref": f"#/components/schemas/{name}"}}},
                }
    return schema
