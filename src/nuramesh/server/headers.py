"""Security headers on every HTTP response, and no CORS (BASELINE A3; R1.22 PR 2, T66).

Pure ASGI so the headers also reach answers made by the guard (a refused Host, a 413). Nothing
here ever adds `Access-Control-*`: the API is same-origin only, and a cross-origin preflight
reaches no route that allows `OPTIONS`.
"""

import base64
import hashlib
import re
from typing import Any

from fastapi.openapi.docs import get_swagger_ui_html
from starlette.responses import HTMLResponse

from nuramesh.server.proxies import TrustedProxies, trusts_peer
from nuramesh.server.setup_page import SETUP_CSP, SETUP_PATH
from nuramesh.server.webapp import WebBundle

# the API answers JSON and nothing a browser should render, frame, script or cache
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
# the web app's page and files (#876, ND394), only on the answers `webapp.WebBundle` serves. The
# same policy `apps/app/csp.ts` builds into the page's <meta> (a test renders that file and
# compares), plus `frame-ancestors`, which only a header can carry. No inline or evaluated script;
# `'unsafe-inline'` is for styles only (the component library injects a <style> element).
_APP_LOOPBACK = "http://127.0.0.1:* http://localhost:* ws://127.0.0.1:* ws://localhost:*"
APP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; font-src 'self' data:; "
    f"connect-src 'self' {_APP_LOOPBACK}; worker-src 'self' blob:; manifest-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
# the interactive docs page loads Swagger UI from a CDN and runs one inline script; it exists only
# on an app with no organization and no `dev_human` (no human can be signed in there). W.05: the
# script is allowed by its sha256 (taken from the page FastAPI really serves), never by
# `'unsafe-inline'`; the page has no inline style. R1.30 review-3 m6: the two CDN files are pinned
# to an exact version, carry a sha384 integrity attribute, and the policy allows exactly those two
# URLs, never the whole CDN (ND370). To bump: download both files, compute
# `openssl dgst -sha384 -binary <file> | openssl base64 -A`, update the four constants together.
DOCS_OPENAPI_URL = "/v1/openapi.json"
SWAGGER_UI_VERSION = "5.34.1"
_SWAGGER_BASE = f"https://cdn.jsdelivr.net/npm/swagger-ui-dist@{SWAGGER_UI_VERSION}"
SWAGGER_JS_URL = f"{_SWAGGER_BASE}/swagger-ui-bundle.js"
SWAGGER_CSS_URL = f"{_SWAGGER_BASE}/swagger-ui.css"
SWAGGER_JS_SRI = "sha384-XFYN47VzPiDY6qPtxYKg0nqUeW/upo20xf1j2SUE7gT/i35FJB4cuS/71Iegzk8Z"
SWAGGER_CSS_SRI = "sha384-4c3vsE5f+trhFyHrFrLM1fFGUn531NIaC7dcqEWIJdoRQJ5CkJbSuSCwXLpaS1KF"


def swagger_ui_page(openapi_url: str) -> HTMLResponse:
    """FastAPI's Swagger UI page with the CDN files pinned and an integrity attribute on each."""
    page = bytes(
        get_swagger_ui_html(
            openapi_url=openapi_url,
            title="NuraMesh API",
            swagger_js_url=SWAGGER_JS_URL,
            swagger_css_url=SWAGGER_CSS_URL,
        ).body
    ).decode()
    for url, sri in ((SWAGGER_JS_URL, SWAGGER_JS_SRI), (SWAGGER_CSS_URL, SWAGGER_CSS_SRI)):
        marker = f'="{url}"'
        if page.count(marker) != 1:
            raise RuntimeError("the Swagger UI page no longer references the pinned file once")
        page = page.replace(marker, f'{marker} integrity="{sri}" crossorigin="anonymous"')
    return HTMLResponse(page)


def inline_script_hash(openapi_url: str) -> str:
    """The CSP source (`'sha256-...'`) of the one inline script of FastAPI's Swagger UI page."""
    page = bytes(swagger_ui_page(openapi_url).body).decode()
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    if len(scripts) != 1 or re.search(r"<style|\sstyle=", page):
        raise RuntimeError("the Swagger UI page no longer has exactly one inline script")
    digest = base64.b64encode(hashlib.sha256(scripts[0].encode()).digest()).decode()
    return f"'sha256-{digest}'"


DOCS_CSP = (
    f"default-src 'none'; script-src 'self' {inline_script_hash(DOCS_OPENAPI_URL)} "
    f"{SWAGGER_JS_URL}; style-src 'self' {SWAGGER_CSS_URL}; "
    "img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
HSTS = "max-age=31536000"
BASE_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "cache-control": "no-store",
}


def last_forwarded(values: list[str]) -> str:
    """The last entry of a forwarded header: all header lines joined with commas, split, and the
    final element trimmed. A proxy appends its own value at the end; earlier lines or entries
    can be sent by the client itself, so only the last one is believed."""
    return ",".join(values).rsplit(",", 1)[-1].strip()


def secure_transport(scope: Any, trusted_proxy: TrustedProxies | bool) -> bool:
    """https, or (only when the socket peer is a trusted proxy) the **last** `X-Forwarded-Proto`
    value across every header line and comma list (`[http, https]` and `"http, https"` are
    secure; `[https, http]` and `"https, http"` are not)."""
    if scope.get("scheme") in ("https", "wss"):
        return True
    client = scope.get("client")
    if not trusts_peer(trusted_proxy, str(client[0]) if client else None):
        return False
    lines = [v.decode("latin-1") for k, v in scope["headers"] if k == b"x-forwarded-proto"]
    return bool(lines) and last_forwarded(lines).lower() == "https"


def header_items(
    scope: Any, trusted_proxy: TrustedProxies | bool, *, docs: bool = False
) -> dict[str, str]:
    """The security headers for a response to this request (lower-case names). The wider docs
    policy is used only when the app serves the docs page (`docs`)."""
    on_docs = docs and scope["path"] == "/v1/docs"
    added = {**BASE_HEADERS, "content-security-policy": DOCS_CSP if on_docs else API_CSP}
    if secure_transport(scope, trusted_proxy):
        added["strict-transport-security"] = HSTS
    return added


class SecurityHeaders:
    """Adds the headers to every `http` response; existing values for them are replaced. Answers
    to unhandled errors are made outside this middleware, so the 500 handler uses
    `header_items` too.

    With a web app (`web`), a 200 to a request that `WebBundle.resolve` names is the page or one
    of its files: it gets the page policy (`APP_CSP`) and that file's cache rule in place of the
    API's two. The decision is made here from the request, never taken from the response, and
    every other answer (a refusal by the guard, any `/v1` path, a 404) keeps the API's headers."""

    def __init__(
        self,
        app: Any,
        *,
        trusted_proxy: TrustedProxies | bool = False,
        docs: bool = False,
        web: WebBundle | None = None,
    ) -> None:
        self.app = app
        self.trusted_proxy = trusted_proxy
        self.docs = docs
        self.web = web

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        base = header_items(scope, self.trusted_proxy, docs=self.docs)
        on_setup = scope["path"] == SETUP_PATH
        page = None if self.web is None else self.web.resolve(scope["method"], scope["path"])

        async def wrapped(message: Any) -> None:
            if message["type"] == "http.response.start":
                added = base
                if page is not None and message["status"] == 200:
                    # #876: the web app answers first (never `/setup`, which stays the server
                    # page), so its policy applies to the app's own pages
                    added = {
                        **base,
                        "content-security-policy": APP_CSP,
                        "cache-control": page.cache_control,
                    }
                elif on_setup and message["status"] == 200:
                    # SA-0.10: the setup page itself (the route answers 200 only with the page)
                    # runs one inline script and one inline style, each allowed by its hash;
                    # every other answer on that path (400, 404, 429) keeps the API policy
                    added = {**base, "content-security-policy": SETUP_CSP}
                kept = [(k, v) for k, v in message["headers"] if k.decode().lower() not in added]
                message["headers"] = [
                    *kept,
                    *((k.encode(), v.encode()) for k, v in added.items()),
                ]
            await send(message)

        await self.app(scope, receive, wrapped)
