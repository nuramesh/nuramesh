"""The `NuraMesh-Version` header (R1.43 C3, spec/cli-api-naming sections 7.2 and 8).

A client that sends `2026-12` asks for the new shapes; no header keeps today's. Any other value
is refused before anything runs with 400 `version_unsupported`. The check is by exact text, on
`/v1/` requests only, and runs before authentication (it reveals nothing about the account).
Every `/v1` response, errors and the 400 included, names the version in the same header; other
paths never carry it."""

from typing import Any, Final

from nuramesh.server.problems import problem_response

HEADER: Final = b"nuramesh-version"
SUPPORTED: Final = ("2026-12",)
CURRENT: Final = SUPPORTED[-1]


class VersionHeader:
    """ASGI: refuses an unknown `NuraMesh-Version` and echoes the version on every /v1 answer."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/v1/"):
            await self.app(scope, receive, send)
            return

        async def tagged(message: Any) -> None:
            if message["type"] == "http.response.start":
                kept = [h for h in message["headers"] if h[0].lower() != HEADER]
                message["headers"] = [*kept, (HEADER, CURRENT.encode())]
            await send(message)

        values = [v for k, v in scope["headers"] if k.lower() == HEADER]
        if values and (len(values) > 1 or values[0].decode("latin-1") not in SUPPORTED):
            response = problem_response(
                400,
                "version_unsupported",
                "the NuraMesh-Version header must be one of: " + ", ".join(SUPPORTED),
            )
            await response(scope, receive, tagged)
            return
        await self.app(scope, receive, tagged)
