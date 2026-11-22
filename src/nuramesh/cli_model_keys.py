"""`nuramesh models key set|rotate|revoke|status PROVIDER` (R1.14c sections 3, 6 and 9; VK39).

These commands are an API client with an owner login (design decision 2026-11-08: never a CLI
without an owner login). They never open a store, a ledger or a secret store: the server does
the work and ledgers it (`model.key_*`), under the same owner-only, MFA and hosted rules as the
app. Nothing here is a second path to a key.

* The owner logs in with an email and a password asked on the terminal without echo (the
  controlling terminal, never stdin, so a piped key is not mistaken for a password). No terminal,
  no login, exit 2. A second factor is asked the same way. An enrolment challenge stops with a
  pointer to the app.
* The key value is read from standard input (`--stdin`), an inherited file descriptor (`--fd N`)
  or a no-echo prompt. There is no `--value`: it is never an argument, never an environment
  variable, never printed, never in an error text, and dropped after the one request.
* The server address is `--server` or `NURAMESH_API_URL` (a plain setting). The login and the key
  only travel over https, or over http to this machine (loopback). No proxy from the environment
  is used and no redirect is followed.
* Output is the key id, version, state and fixed server codes. A server text that contains the key
  is not printed.
"""

import argparse
import contextlib
import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from nuramesh import cli_prompts
from nuramesh.engine.key_ops import REASON_MAX, REASON_MIN, reason_ok

OK, USAGE, REFUSED = 0, 2, 3
DEFAULT_SERVER = "http://127.0.0.1:8765"
SERVER_ENV = "NURAMESH_API_URL"
VALUE_READ_LIMIT = 1025  # one past the longest valid key text (spec 6 step 1: 10 to 512)
TIMEOUT_SECONDS = 60.0  # a set runs a provider connection test
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
ALIVE_STATES = frozenset({"current", "grace", "pending"})
SERVER_TEXT = (
    "--server needs an https URL, or an http URL for this machine (127.0.0.1), no login in it"
)


class Wire(Protocol):
    """One HTTP exchange: `(status, JSON object)`. The real one keeps the session cookies."""

    def request(
        self, method: str, path: str, body: dict[str, Any] | None, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a: Any, **_k: Any) -> None:
        return None


class UrlWire:
    """The real transport: no proxy from the environment, no redirect, an in-memory cookie jar."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect,
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(
        self, method: str, path: str, body: dict[str, Any] | None, headers: dict[str, str]
    ) -> tuple[int, dict[str, Any]]:
        data = None if body is None else json.dumps(body).encode()
        head = {"Accept": "application/json", **headers}
        if data is not None:
            head["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=head, method=method)
        try:
            with self._opener.open(req, timeout=TIMEOUT_SECONDS) as response:
                status, raw = response.status, response.read(1 << 20)
        except urllib.error.HTTPError as exc:  # a status answer (4xx, 5xx, an unfollowed 3xx)
            status, raw = exc.code, exc.read(1 << 20)
        except (urllib.error.URLError, OSError, ValueError):
            return 0, {}
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return status, parsed if isinstance(parsed, dict) else {}


def open_wire(base: str) -> Wire:
    return UrlWire(base)


def have_terminal() -> bool:
    """A controlling terminal that can be asked (and no `CI`)."""
    if os.environ.get("CI"):
        return False
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(fd)
    return True


def ask_tty(prompt: str, *, secret: bool) -> str:
    """One answer typed on the controlling terminal (never stdin); not echoed when `secret`."""
    if secret:
        import getpass

        return getpass.getpass(prompt, stream=sys.stderr)
    with open("/dev/tty", "r+", encoding="utf-8") as tty:
        tty.write(prompt)
        tty.flush()
        return tty.readline().rstrip("\r\n")


def server_of(args: argparse.Namespace) -> str | None:
    """The checked server address, or None after printing why."""
    given = getattr(args, "server", None) or os.environ.get(SERVER_ENV) or DEFAULT_SERVER
    try:
        parts = urllib.parse.urlsplit(given)
        host, _ = parts.hostname, parts.port
    except ValueError:
        host = None
    ok = (
        host is not None
        and parts.scheme in ("http", "https")
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
        and parts.path in ("", "/")
        and (parts.scheme == "https" or host in LOOPBACK)
    )
    if not ok:
        print(SERVER_TEXT, file=sys.stderr)
        return None
    return f"{parts.scheme}://{parts.netloc}"


def read_value(args: argparse.Namespace) -> str | None:
    """The key text from `--fd`, `--stdin` or the no-echo prompt; one line, at most
    `VALUE_READ_LIMIT` characters read. None (after printing why) when it cannot be asked."""
    fd = getattr(args, "fd", None)
    if fd is not None:
        try:
            with os.fdopen(fd, "r", encoding="utf-8", closefd=True) as stream:
                line = stream.readline(VALUE_READ_LIMIT)
        except (OSError, ValueError, UnicodeDecodeError):
            print("the key could not be read from that file descriptor", file=sys.stderr)
            return None
        return line.rstrip("\r\n")
    if getattr(args, "stdin", False):
        return sys.stdin.readline(VALUE_READ_LIMIT).rstrip("\r\n")
    if getattr(args, "yes", False):
        print("missing the key: pass --stdin or --fd N (--yes never asks)", file=sys.stderr)
        return None
    return ask_tty("Key (not shown): ", secret=True)


def _clean(text: Any, secret: str | None) -> str:
    """A server text that is safe to print: printable, short, and never holding the key."""
    raw = str(text)
    full = "".join(ch for ch in raw if ch.isprintable())
    if secret and (
        secret in raw or secret in full
    ):  # check the whole text: a cut must not hide half a key
        return ""
    return full[:200]


def _fail(status: int, body: dict[str, Any], secret: str | None = None) -> int:
    if status == 0:
        print("the server could not be reached", file=sys.stderr)
        return REFUSED
    code = _clean(body.get("code", f"http_{status}"), secret)
    detail = _clean(body.get("detail", ""), secret)
    print(f"{code}: {detail}" if detail else code, file=sys.stderr)
    return USAGE if status == 422 and code != "key_test_failed" else REFUSED


class Login:
    """An owner session on one wire: `csrf` for the writes and the organization id."""

    def __init__(self, wire: Wire, csrf: str, org: str) -> None:
        self.wire, self.csrf, self.org = wire, csrf, org

    def call(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        return self.wire.request(method, path, body, {"X-CSRF-Token": self.csrf})

    def logout(self) -> None:
        with contextlib.suppress(Exception):
            self.call("POST", "/v1/auth/logout", {})


def _login(wire: Wire, args: argparse.Namespace) -> Login | int:
    email = getattr(args, "email", None) or ask_tty("Email: ", secret=False)
    password = ask_tty("Password: ", secret=True)
    status, body = wire.request(
        "POST", "/v1/auth/login", {"email": email, "password": password}, {}
    )
    del password
    if status == 200 and body.get("status") == "mfa_enroll_required":
        print("this owner has no second factor yet: enroll in the app first", file=sys.stderr)
        return REFUSED
    if status == 200 and body.get("status") == "mfa_required":
        code = ask_tty("MFA code: ", secret=True)
        status, body = wire.request(
            "POST",
            "/v1/auth/login/mfa",
            {"challenge": str(body.get("challenge", "")), "code": code},
            {},
        )
        del code
    user = body.get("user")
    csrf = body.get("csrf_token")
    if status != 200 or not isinstance(csrf, str) or not isinstance(user, dict):
        print("login failed", file=sys.stderr)
        return REFUSED
    return Login(wire, csrf, str(user.get("org_id", "")))


def _path(args: argparse.Namespace) -> str:
    quoted = urllib.parse.quote(args.provider, safe="")
    path = f"/v1/models/connections/{quoted}/key"
    if getattr(args, "endpoint_id", None):
        path += "?endpoint_id=" + urllib.parse.quote(args.endpoint_id, safe="")
    return path


def _versions(login: Login, path: str) -> tuple[int, list[dict[str, Any]], dict[str, Any]]:
    status, body = login.call("GET", path)
    found = body.get("versions") if status == 200 else None
    return (
        status,
        [v for v in found if isinstance(v, dict)] if isinstance(found, list) else [],
        body,
    )


def _session(args: argparse.Namespace) -> tuple[Wire, Login] | int:
    base = server_of(args)
    if base is None:
        return USAGE
    if not have_terminal():
        print(
            "the owner login needs a terminal (the password and any MFA code are typed there); "
            "an agent or script has no owner session",
            file=sys.stderr,
        )
        return USAGE
    wire = open_wire(base)
    login = _login(wire, args)
    if isinstance(login, int):
        return login
    return wire, login


def _reason_problem(reason: str | None, *, required: bool) -> str | None:
    if reason is None:
        return "a reason is required (--reason, 3 to 500 characters)" if required else None
    if not reason_ok(reason):
        return f"--reason must be {REASON_MIN} to {REASON_MAX} characters"
    return None


def _change(args: argparse.Namespace, *, rotate: bool) -> int:
    problem = _reason_problem(args.reason, required=False)
    if problem is not None:
        print(problem, file=sys.stderr)
        return USAGE
    started = _session(args)
    if isinstance(started, int):
        return started
    login = started[1]
    secret: str | None = None
    try:
        path = _path(args)
        status, versions, body = _versions(login, path)
        if status not in (200, 404):
            return _fail(status, body)
        has_key = any(v.get("state") in ALIVE_STATES for v in versions)
        if rotate and not has_key:
            print("this connection has no stored key: use `models key set`", file=sys.stderr)
            return REFUSED
        if not rotate and has_key:
            print("this connection already has a key: use `models key rotate`", file=sys.stderr)
            return REFUSED
        if not rotate and args.reason is None:
            print(_reason_problem(None, required=True), file=sys.stderr)
            return USAGE
        secret = read_value(args)
        if secret is None:
            return USAGE
        payload: dict[str, Any] = {"value": secret}
        if args.reason is not None:
            payload["reason"] = args.reason
        if args.grace_seconds is not None:
            payload["grace_seconds"] = args.grace_seconds
        status, body = login.call("PUT", path, payload)
        del payload
        if status == 422 and body.get("code") == "key_test_failed":
            why = _clean(body.get("failure_code"), secret)
            print(
                f"key_test_failed: the connection test failed ({why}); the connection is unchanged",
                file=sys.stderr,
            )
            return REFUSED
        if status not in (200, 201):
            return _fail(status, body, secret)
        print(
            f"key {_clean(body.get('key_id'), secret)} version {body.get('version')} "
            f"is {_clean(body.get('state'), secret)}"
        )
        return OK
    finally:
        secret = None
        login.logout()


def cmd_set(args: argparse.Namespace) -> int:
    return _change(args, rotate=False)


def cmd_rotate(args: argparse.Namespace) -> int:
    return _change(args, rotate=True)


def cmd_revoke(args: argparse.Namespace) -> int:
    problem = _reason_problem(args.reason, required=True)
    if problem is not None:
        print(problem, file=sys.stderr)
        return USAGE
    started = _session(args)
    if isinstance(started, int):
        return started
    login = started[1]
    try:
        if args.confirm != login.org:  # the organization id typed with --confirm is the answer
            stop = cli_prompts.confirm_org(args, login.org, "models key revoke")
            if stop is not None:
                return stop
        body: dict[str, Any] = {"reason": args.reason}
        if args.version is not None:
            body["version"] = args.version
        status, done = login.call("DELETE", _path(args), body)
        if status != 200:
            return _fail(status, done)
        key_id, gone = _clean(done.get("key_id"), None), done.get("revoked_versions")
        print(f"key {key_id}: revoked versions {gone}")
        return OK
    finally:
        login.logout()


def cmd_status(args: argparse.Namespace) -> int:
    started = _session(args)
    if isinstance(started, int):
        return started
    login = started[1]
    try:
        status, versions, body = _versions(login, _path(args))
        if status != 200:
            return _fail(status, body)
        if getattr(args, "output", None) == "json":
            advice = {k: body[k] for k in ("rotate_advised", "age_days") if k in body}
            print(json.dumps({"versions": versions, **advice}, indent=2))
            return OK
        for item in sorted(versions, key=lambda v: int(v.get("version", 0))):
            extra = "".join(
                f"  {name} {item[name]}"
                for name in ("created_at", "activated_at", "grace_until")
                if item.get(name)
            )
            print(f"version {item.get('version')}  {_clean(item.get('state'), None)}{extra}")
        if body.get("rotate_advised"):
            print(f"advice: the current key is {body.get('age_days')} days old; rotate it")
        return OK
    finally:
        login.logout()


_OPTION_NAME = re.compile(r"--[a-z][a-z-]{0,30}")
_WORDS = frozenset({"models", "key", "set", "rotate", "revoke", "status"})


def is_key_command(argv: list[str]) -> bool:
    """True when the typed words are a `models key ...` command (before any option)."""
    words = [w for w in argv if not w.startswith("-")]
    return words[:2] == ["models", "key"]


def scrub_usage_error(message: str, argv: list[str]) -> str:
    """An argparse error text with no typed value in it (VK39: nothing echoes a key).

    An "unrecognized arguments" list shows option names only; any other message has every typed
    value (the word after an option, the part after `=`, a stray word) replaced.
    """
    if message.startswith("unrecognized arguments:"):
        names = []
        for token in message.partition(":")[2].split():
            name = token.partition("=")[0]
            names.append(name if _OPTION_NAME.fullmatch(name) else "<hidden>")
        return "unrecognized arguments: " + " ".join(names) + " (values are not shown)"
    hidden = {
        t.partition("=")[2] if t.startswith("-") else t
        for t in argv
        if (t not in _WORDS and not t.startswith("-")) or (t.startswith("-") and "=" in t)
    }
    for value in sorted(filter(None, hidden), key=len, reverse=True):
        message = message.replace(value, "<hidden>")
    return message


def register(ms: Any) -> None:
    """Add `models key ...` to the `models` subparsers."""
    key = ms.add_parser("key", help="stored model keys: set, rotate, revoke, status (owner login)")
    ks = key.add_subparsers(dest="key_command", required=True)
    for name, func, helptext in (
        ("set", cmd_set, "store the first key of a connection"),
        ("rotate", cmd_rotate, "store a new version; the old one stays for the grace time"),
        ("revoke", cmd_revoke, "revoke the key (all versions, or --version N)"),
        ("status", cmd_status, "the key's versions and states (never the value)"),
    ):
        p = ks.add_parser(name, help=helptext)
        p.add_argument("provider")
        p.add_argument("--endpoint-id", dest="endpoint_id", help="the connection's id")
        p.add_argument(
            "--server", help=f"the API address (default ${SERVER_ENV} or {DEFAULT_SERVER})"
        )
        p.add_argument("--email", help="the owner's email (asked on the terminal if missing)")
        if name in ("set", "rotate"):
            source = p.add_mutually_exclusive_group()
            source.add_argument(
                "--stdin", action="store_true", help="read the key from standard input"
            )
            source.add_argument(
                "--fd", type=int, metavar="N", help="read the key from this open file descriptor"
            )
            p.add_argument("--grace-seconds", dest="grace_seconds", type=int, default=None)
            p.add_argument("--reason", help="why (3 to 500 characters; needed for the first key)")
        if name == "revoke":
            p.add_argument("--version", type=int, default=None)
            p.add_argument("--reason", help="why (3 to 500 characters; required)")
            p.add_argument("--confirm", metavar="ORG", help="the organization id, to confirm")
        p.set_defaults(func=func)
