"""The web app at `/`, served by the API from the wheel (#876, ND394).

The release build copies the built single-page app into `nuramesh/web/` with a `manifest.json`
that lists every file with its sha256 and size (`scripts/release/build_web.py`). A checkout, and
a wheel built without that step, have no such folder: then nothing is served and the API is
exactly what it was.

**A strict allow-list, decided at start.** `load_web_bundle` reads the folder once, refuses it
whole if anything is off (a file the manifest does not list, a changed byte, a symlink, a name
outside the rules, a member type that is not listed) and keeps the bytes in memory. A request is
answered from that table by exact name: no path is ever joined to a folder and no file is opened
while serving, so there is no directory listing, no traversal, no dotfile and no source map to
reach. The page itself (`index.html`) is the answer for `/` and for the routes the app's router
declares (`WEB_ROUTES`), never for an unknown path. `/v1` is never answered here.

Only `GET` and `HEAD` are answered. The files are public (the sign-in page must load before
anyone is signed in) and hold no organization data. The security headers are added by
`headers.SecurityHeaders`, which asks the same `WebBundle.resolve`.
"""

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

MANIFEST_NAME = "manifest.json"
INDEX_NAME = "index.html"
# where the release build puts the app: `nuramesh/web/` in the installed package
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# the member types of `scripts/release/allowlist.json` (`web.suffixes`; a test keeps them equal)
# and the media type each is served with. `X-Content-Type-Options: nosniff` holds browsers to it.
CONTENT_TYPES: Mapping[str, str] = MappingProxyType(
    {
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".ico": "image/x-icon",
        ".js": "text/javascript; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".txt": "text/plain; charset=utf-8",
        ".webmanifest": "application/manifest+json",
        ".woff2": "font/woff2",
    }
)
# the same limits as the release allow-list (`web.max_*`): a folder beyond them is not ours
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 6_000_000
MAX_FILES = 200
MAX_MANIFEST_BYTES = 200_000
# the routes of `apps/app/src/app/routes.tsx` (a test keeps the two lists equal); `:name` is one
# path segment. `/` is the page too. Anything else is not the page: a 404 from the API.
# `/setup` is deliberately NOT here: a hard load of it is the API's own first-owner page
# (`server/setup_page.py`: 404 rules, hash-pinned policy, rate limit, token read from the URL
# fragment). The app's client-side `/setup` route (from the login screen) is unaffected.
# Every route here is a GET/HEAD-only static page (`index.html`) with no data access: data is
# read through `/v1`, which has its own route-matrix rows (a test enumerates this list).
WEB_ROUTES: tuple[str, ...] = (
    "/builder",
    "/templates",
    "/runs",
    "/runs/:id",
    "/runs/:runId/tasks/:taskId",
    "/agents",
    "/approvals",
    "/approvals/:id",
    "/help/approving",
    "/autonomy",
    "/budgets",
    "/models",
    "/models/:tab",
    "/ledger",
    "/ledger/:entry",
    "/dashboards",
    "/dashboards/:view",
    "/reports/downgrade",
    "/settings",
    "/login",
    "/verify-email",
    "/accept-invite",
    "/forgot-password",
    "/reset-password",
    "/revoke-token",
    "/start",
)
NO_STORE = "no-store"  # the page: always the current one, and never kept (links carry tokens)
REVALIDATE = "no-cache"  # a file whose name carries no content hash
IMMUTABLE = "public, max-age=31536000, immutable"  # a build-hashed file: the name changes with it

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# the bundler writes every file under assets/ as <name>-<8 character content hash>.<suffix>
_HASHED = re.compile(r"^assets/[A-Za-z0-9._-]+-[A-Za-z0-9_-]{8}\.[a-z0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PARAM = re.compile(r"^[^/\x00-\x1f\x7f]+$")


class WebBundleError(Exception):
    """The web folder exists but is not exactly what its manifest says."""


@dataclass(frozen=True)
class WebFile:
    """One answer: the bytes and the two headers that depend on the file."""

    body: bytes
    content_type: str
    cache_control: str


def name_problem(name: str) -> str | None:
    """Why `name` (a path relative to the web folder) may not be served, or None."""
    parts = name.split("/")
    if any(part.startswith(".") for part in parts):
        return "a name that starts with a dot"
    if not all(_SEGMENT.fullmatch(part) for part in parts):
        return "not a plain name (letters, digits, dot, dash, underscore; parts joined by /)"
    if parts[0] == "v1":
        return "under v1, which belongs to the API"
    if name == MANIFEST_NAME:
        return "the manifest itself is not a served file"
    if os.path.splitext(parts[-1])[1] not in CONTENT_TYPES:
        return "not a listed member type"
    return None


def _cache_control(name: str) -> str:
    if name == INDEX_NAME:
        return NO_STORE
    return IMMUTABLE if _HASHED.fullmatch(name) else REVALIDATE


def _is_route(path: str) -> bool:
    segments = path[1:].split("/")
    for parts in _ROUTE_PARTS:
        if len(parts) == len(segments) and all(
            (_PARAM.fullmatch(seg) is not None and seg not in (".", ".."))
            if part.startswith(":")
            else part == seg
            for part, seg in zip(parts, segments, strict=True)
        ):
            return True
    return False


_ROUTE_PARTS = tuple(tuple(route[1:].split("/")) for route in WEB_ROUTES)
if any(parts[0] == "v1" for parts in _ROUTE_PARTS):  # pragma: no cover (a guard for an edit)
    raise RuntimeError("a web route under /v1 would shadow the API")


@dataclass(frozen=True)
class WebBundle:
    """The files of the web app, checked against their manifest and held in memory."""

    files: Mapping[str, WebFile]

    @property
    def index(self) -> WebFile:
        return self.files[INDEX_NAME]

    def resolve(self, method: str, path: str) -> WebFile | None:
        """What a request is answered with, or None when it is not for the web app.

        `path` is the decoded request path. A listed file by its exact name, the page for `/`
        and the known routes, and nothing else; never anything under `/v1`."""
        if method not in ("GET", "HEAD"):
            return None
        return self.lookup(path)

    def lookup(self, path: str) -> WebFile | None:
        """The file or page a web path names, whatever the method; None for anything else.

        One trailing slash is accepted on a known route (`/login/`), never on a file."""
        if not path.startswith("/") or path == "/v1" or path.startswith("/v1/"):
            return None
        if path == "/":
            return self.index
        found = self.files.get(path[1:])
        if found is not None:
            return found
        if _is_route(path):
            return self.index
        if path.endswith("/") and not path[:-1].endswith("/") and _is_route(path[:-1]):
            return self.index
        return None


def _read_manifest(folder: Path) -> dict[str, tuple[str, int]]:
    path = folder / MANIFEST_NAME
    if path.is_symlink() or not path.is_file():
        raise WebBundleError(f"{MANIFEST_NAME} is missing")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise WebBundleError(f"{MANIFEST_NAME} is too large")
    try:
        manifest = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WebBundleError(f"{MANIFEST_NAME}: the manifest is not JSON") from exc
    if not isinstance(manifest, dict) or set(manifest) != {"version", "files"}:
        raise WebBundleError(f"{MANIFEST_NAME}: the manifest has not exactly version and files")
    if manifest["version"] != 1 or isinstance(manifest["version"], bool):
        raise WebBundleError(f"{MANIFEST_NAME}: an unknown version")
    files = manifest["files"]
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES:
        raise WebBundleError(f"{MANIFEST_NAME}: the manifest lists no files or too many")
    listed: dict[str, tuple[str, int]] = {}
    for name, entry in files.items():
        problem = name_problem(name)
        if problem is not None:
            raise WebBundleError(f"{name!r}: {problem}")
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "size"}
            or not isinstance(entry["sha256"], str)
            or not _SHA256.fullmatch(entry["sha256"])
            or not isinstance(entry["size"], int)
            or isinstance(entry["size"], bool)
            or not 0 <= entry["size"] <= MAX_FILE_BYTES
        ):
            raise WebBundleError(f"{name!r}: the manifest entry is not a sha256 and a size")
        listed[name] = (entry["sha256"], entry["size"])
    if INDEX_NAME not in listed:
        raise WebBundleError(f"{INDEX_NAME} is not in the manifest")
    if sum(size for _, size in listed.values()) > MAX_TOTAL_BYTES:
        raise WebBundleError("the files are larger than the size budget")
    return listed


def _on_disk(folder: Path) -> set[str]:
    """Every file in the folder, as a relative name; a symlink anywhere is refused."""
    found: set[str] = set()
    for here, dirs, names in os.walk(folder, followlinks=False):
        for name in (*dirs, *names):
            entry = Path(here) / name
            if entry.is_symlink():
                raise WebBundleError(f"{entry.relative_to(folder).as_posix()!r}: a symlink")
        for name in names:
            entry = Path(here) / name
            if not entry.is_file():
                raise WebBundleError(
                    f"{entry.relative_to(folder).as_posix()!r}: not a regular file"
                )
            found.add(entry.relative_to(folder).as_posix())
    return found


def load_web_bundle(directory: Path | None = None) -> WebBundle | None:
    """The web app of this install, or None when there is none (no folder).

    `directory` defaults to the package's own `web/` folder. A folder that exists must match its
    manifest exactly, or `WebBundleError` says what does not: nothing is served from a folder
    that was changed after the build."""
    folder = WEB_DIR if directory is None else directory
    if not folder.exists() and not folder.is_symlink():
        return None
    if folder.is_symlink() or not folder.is_dir():
        raise WebBundleError("the web folder is not a plain folder")
    listed = _read_manifest(folder)
    present = _on_disk(folder) - {MANIFEST_NAME}
    for name in sorted(present - set(listed)):
        raise WebBundleError(f"{name!r}: a file the manifest does not list")
    for name in sorted(set(listed) - present):
        raise WebBundleError(f"{name!r}: listed in the manifest but missing")
    files: dict[str, WebFile] = {}
    for name, (digest, size) in sorted(listed.items()):
        path = folder / name
        if path.stat().st_size != size:
            raise WebBundleError(f"{name!r}: the size differs from the manifest")
        body = path.read_bytes()
        if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
            raise WebBundleError(f"{name!r}: the bytes differ from the manifest")
        suffix = os.path.splitext(name)[1]
        files[name] = WebFile(body, CONTENT_TYPES[suffix], _cache_control(name))
    return WebBundle(MappingProxyType(files))


class WebApp:
    """ASGI middleware, inside the guard (Host allow-list, TLS rule) and outside the router:
    answers what `WebBundle.resolve` names and passes everything else on untouched."""

    def __init__(self, app: Any, bundle: WebBundle) -> None:
        self.app = app
        self.bundle = bundle

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        found = (
            self.bundle.resolve(scope["method"], scope["path"]) if scope["type"] == "http" else None
        )
        if (
            found is None
            and scope["type"] == "http"
            and scope["method"] not in ("GET", "HEAD")
            and self.bundle.lookup(scope["path"]) is not None
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [
                        (b"allow", b"GET, HEAD"),
                        (b"content-length", b"0"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if found is None:
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", found.content_type.encode()),
                    (b"content-length", str(len(found.body)).encode()),
                    (b"cache-control", found.cache_control.encode()),
                ],
            }
        )
        body = b"" if scope["method"] == "HEAD" else found.body
        await send({"type": "http.response.body", "body": body})
