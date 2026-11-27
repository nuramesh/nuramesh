"""`nuramesh doctor`: checks this machine for what `serve`, `try` and the demo need (#991).

One line per check: `PASS`, `WARN` or `FAIL`, a fixed message and, unless it passes, a fix hint.
Exit 0 when nothing failed (warnings do not fail), 1 when a check failed. `--output json` prints
`{"ok", "summary", "checks": [{"id", "status", "message", "hint"}]}`. Nothing here prints a secret,
a connection URL or a file path from the environment: a check names the variable, never its value.
"""

import argparse
import importlib.metadata
import json
import os
import shutil
import stat
import sys
from datetime import date
from typing import Any

from nuramesh.config.shipped_prices import (
    STALE_AFTER_DAYS,
    ShippedList,
    ShippedPriceError,
    load_shipped,
    today_utc,
)
from nuramesh.env_bool import EnvBoolError, env_bool
from nuramesh.secret_env import SecretEnvError, database_url

OK, FAILED = 0, 1
MIN_PYTHON = (3, 12)
DEFAULT_PORT = 8765
FAIL_FREE = 100 * 1024**2
WARN_FREE = 1024**3
# extra name -> the distribution that proves it is installed
EXTRAS = {
    "yaml": "pyyaml",
    "server": "fastapi",
    "content": "cryptography",
    "postgres": "psycopg",
    "langgraph": "langgraph",
    "azure": "azure-identity",
}


class ProbeError(Exception):
    """A probe failed with a message that is safe to show (no URL, no path, no secret)."""


def container_runtime() -> str | None:
    """The container runtime that answers (Podman or Docker), or None (tests replace this)."""
    from nuramesh.engine.sandbox import detect_runtime

    path = detect_runtime()
    return None if path is None else os.path.basename(path)


def module_present(name: str) -> bool:
    """Whether the distribution `name` is installed (tests replace this)."""
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def database_has_schema(dsn: str) -> bool:
    """Connect read-only and say whether the ledger tables exist (tests replace this)."""
    try:
        from nuramesh.stores.base import StoreError
        from nuramesh.stores.postgres import PostgresStore

        return bool(PostgresStore.has_schema(dsn))
    except ImportError:
        raise ProbeError("the postgres extra is not installed") from None
    except StoreError as exc:
        raise ProbeError(str(exc)) from None


def _check(cid: str, status: str, message: str, hint: str = "") -> dict[str, str]:
    return {"id": cid, "status": status, "message": message, "hint": hint}


def check_python() -> dict[str, str]:
    have = ".".join(str(n) for n in sys.version_info[:3])
    need = ".".join(str(n) for n in MIN_PYTHON)
    if tuple(sys.version_info[:2]) >= MIN_PYTHON:
        return _check("python", "PASS", f"Python {have} (needs {need} or newer)")
    return _check(
        "python", "FAIL", f"Python {have} is too old (needs {need} or newer)",
        f"install Python {need} or newer, for example `uv python install {need}`",
    )  # fmt: skip


def check_port(host: str, port: int) -> dict[str, str]:
    from nuramesh.cli import port_is_free  # the approved module for local socket use

    if not port_is_free(host, port):
        return _check(
            "port", "FAIL", f"{host}:{port} is not free",
            f"stop what uses it, or pass another port: `nuramesh serve TEAM --port {port + 1}`",
        )  # fmt: skip
    return _check("port", "PASS", f"{host}:{port} is free")


def check_database(network: bool = False) -> dict[str, str]:
    try:
        url = database_url()  # the plain variable or its _FILE twin
    except SecretEnvError as exc:
        return _check(
            "database", "FAIL", str(exc), "fix the file or unset one of the two"
        )  # fmt: skip
    if not url:
        try:
            required = env_bool("NURAMESH_REQUIRE_DATABASE")
        except EnvBoolError as exc:  # a malformed guard flag fails, it never passes
            return _check(
                "database", "FAIL", str(exc),
                "set NURAMESH_REQUIRE_DATABASE to 1 or 0, or unset it",
            )  # fmt: skip
        if required:
            return _check(
                "database", "FAIL", "NURAMESH_REQUIRE_DATABASE is on but no NURAMESH_DATABASE_URL",
                "set NURAMESH_DATABASE_URL to a PostgreSQL URL, or unset NURAMESH_REQUIRE_DATABASE",
            )  # fmt: skip
        return _check("database", "PASS", "no NURAMESH_DATABASE_URL: runs use a local SQLite file")
    if not network:  # the probe connects to the host in the URL, so it needs the flag
        return _check(
            "database", "SKIPPED", "NURAMESH_DATABASE_URL is set: the connection is not tested",
            "not run: add --network-checks to test the database connection",
        )  # fmt: skip
    try:
        has = database_has_schema(url)
    except ProbeError as exc:
        return _check(
            "database", "FAIL", f"the database is not reachable: {exc}",
            "check NURAMESH_DATABASE_URL, that PostgreSQL is running, and the postgres extra "
            "(`uv sync --extra postgres`)",
        )  # fmt: skip
    if has:
        return _check("database", "PASS", "the database is reachable and has the ledger tables")
    return _check(
        "database", "WARN", "the database is reachable but has no ledger tables yet",
        "run `nuramesh db migrate`",
    )  # fmt: skip


def check_auth_key() -> dict[str, str]:
    name = "NURAMESH_AUTH_KEY_FILE"
    given = os.environ.get(name)
    if not given:
        return _check(
            "auth_key", "WARN", f"{name} is not set: MFA enrolment is off",
            f"for MFA, put a 32-byte key in a mode 0600 file and set {name} to its path",
        )  # fmt: skip
    try:
        info = os.stat(given)
    except OSError:
        return _check(
            "auth_key", "FAIL", f"{name} names a file that cannot be read",
            f"point {name} at an existing file with mode 0600",
        )  # fmt: skip
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        return _check(
            "auth_key", "FAIL",
            f"the file named by {name} is not a regular file closed to group and other users",
            "run `chmod 600` on it (no group or other permissions)",
        )  # fmt: skip
    return _check("auth_key", "PASS", f"{name} names a file with a safe mode")


# a variable that says runs use a team server or a real model, so commands really run
REAL_MODE_VARS = (
    "NURAMESH_DATABASE_URL",
    "NURAMESH_DATABASE_URL_FILE",
    "NURAMESH_REQUIRE_DATABASE",
    "NURAMESH_ANTHROPIC_KEY_FILE",
    "NURAMESH_OPENAI_COMPATIBLE_KEYS",
    "NURAMESH_SECRET_DIR",  # stored model keys (the app and API real-model path)
    "NURAMESH_SECRET_STORE",
)


def real_mode_variable() -> str | None:
    """The name (never the value) of the setting that makes runs execute real commands."""
    for name in REAL_MODE_VARS:
        value = os.environ.get(name)
        if not value:
            continue
        if name == "NURAMESH_REQUIRE_DATABASE":
            try:
                if not env_bool(name):
                    continue
            except EnvBoolError:
                return name  # a malformed flag counts as real: fail closed
        if name == "NURAMESH_SECRET_STORE" and value == "fake":
            continue  # the in-memory trial store holds no real key
        return name
    return None


def check_docker() -> dict[str, str]:
    found = container_runtime()
    if found is not None:
        return _check("docker", "PASS", f"container runtime {found} is available")
    real = real_mode_variable()
    if real is not None:  # runs stay fail-closed: they refuse without a sandbox
        return _check(
            "docker", "FAIL", f"no container runtime (Podman or Docker); {real} is set",
            "install Podman (`brew install podman`) or Colima; runs here execute real commands "
            "and refuse without a sandbox",
        )  # fmt: skip
    return _check(
        "docker", "WARN", "no container runtime (Podman or Docker) answered",
        "install Podman (`brew install podman`) or Colima; without one the test gate refuses",
    )  # fmt: skip


# the sandbox service answers one of these codes (core.sandbox_service); doctor shows only these
_SANDBOX_REFUSALS = {
    "sandbox_rootful": (
        "the sandbox host is not rootless (its user namespace is the host's), so it is refused",
        "run the sandbox service rootless (Podman rootless, or Docker with user namespaces)",
    ),
    "sandbox_unlimited": (
        "the sandbox service has no finite memory, CPU or process limit",
        "set memory, CPU and pids limits on the sandbox container (the shipped Compose stack does)",
    ),
    "sandbox_selftest_failed": (
        "the sandbox service self-test failed, so it refuses every command",
        "check the sandbox service log and the host kernel and user namespace settings",
    ),
    "sandbox_caps_too_small": (
        "the sandbox service limits are smaller than a command needs",
        "raise the sandbox container limits",
    ),
    "no answer": (
        "the sandbox service did not answer the health probe",
        "start the sandbox service and check that the socket is shared with this process",
    ),
}
# a variable that says runs use a team server or a real model, so commands really run
_SANDBOX_REAL_VARS = (
    "NURAMESH_DATABASE_URL", "NURAMESH_DATABASE_URL_FILE", "NURAMESH_REQUIRE_DATABASE",
    "NURAMESH_ANTHROPIC_KEY_FILE", "NURAMESH_OPENAI_COMPATIBLE_KEYS",
)  # fmt: skip


def sandbox_real_mode_variable() -> str | None:
    """The name (never the value) of a setting that makes runs execute real commands."""
    for name in _SANDBOX_REAL_VARS:
        value = os.environ.get(name)
        if not value:
            continue
        if name == "NURAMESH_REQUIRE_DATABASE":
            try:
                if not env_bool(name):
                    continue
            except EnvBoolError:
                pass  # a malformed flag fails closed: treat it as real mode (#1722)
        return name
    return None


def sandbox_service_state(path: str, uid: int | None) -> str:
    """Ask the sandbox service for its health state (`ready`, a refusal code or `no answer`);
    tests replace this."""
    from nuramesh.engine.sandbox_remote import RemoteSandbox

    return RemoteSandbox(path, peer_uid=uid).health()


def check_sandbox() -> dict[str, str]:
    """The sandbox service is ready and rootless. FAIL only when this configuration runs real
    commands (team server or real model); with the fake model it is a WARN, never a FAIL."""
    real = sandbox_real_mode_variable()
    bad = "FAIL" if real is not None else "WARN"
    why = (
        f"; {real} is set, so runs execute real commands"
        if real
        else " (fake model: runs use no real commands)"
    )
    path = os.environ.get("NURAMESH_SANDBOX_SOCKET", "")
    fix = "set NURAMESH_SANDBOX_SOCKET to the sandbox service socket"
    if not path:
        # no service is configured, so none is probed: a host runtime may serve the commands
        # (the container-runtime check covers it) and runs refuse without any sandbox (#1846)
        return _check(
            "sandbox", "WARN",
            "NURAMESH_SANDBOX_SOCKET is not set: no sandbox service is used" + why,
            "if you run the Compose sandbox service, set NURAMESH_SANDBOX_SOCKET to its socket",
        )  # fmt: skip
    if not path.startswith("/"):
        return _check("sandbox", bad, "NURAMESH_SANDBOX_SOCKET is not an absolute path" + why, fix)
    uid_text = os.environ.get("NURAMESH_SANDBOX_UID", "")
    uid = int(uid_text) if uid_text.isdigit() else None
    try:
        code = sandbox_service_state(path, uid)
    except (OSError, ValueError):  # the message may name a path: show a fixed one
        code = "no answer"
    if code == "ready":
        return _check("sandbox", "PASS", "the sandbox service is ready and rootless")
    message, hint = _SANDBOX_REFUSALS.get(code, _SANDBOX_REFUSALS["no answer"])
    return _check("sandbox", bad, message + why, hint)


def free_bytes() -> int:
    """Free bytes where runs write (the current directory; tests replace this)."""
    return shutil.disk_usage(os.getcwd()).free


def check_email() -> dict[str, str]:
    host = os.environ.get("NURAMESH_SMTP_HOST")
    if not host:
        return _check(
            "email", "WARN", "NURAMESH_SMTP_HOST is not set: email is off",
            "for sign-up mails set NURAMESH_SMTP_HOST, NURAMESH_SMTP_FROM, NURAMESH_PUBLIC_URL",
        )  # fmt: skip
    if not os.environ.get("NURAMESH_SMTP_FROM"):
        return _check(
            "email", "FAIL", "NURAMESH_SMTP_HOST is set but NURAMESH_SMTP_FROM is not",
            "set NURAMESH_SMTP_FROM to the sender address",
        )  # fmt: skip
    if not os.environ.get("NURAMESH_PUBLIC_URL"):
        return _check(
            "email", "FAIL", "NURAMESH_SMTP_HOST is set but NURAMESH_PUBLIC_URL is not",
            "the server needs the public URL for the links in mails; set NURAMESH_PUBLIC_URL",
        )  # fmt: skip
    security = os.environ.get("NURAMESH_SMTP_SECURITY", "starttls")
    if security not in ("starttls", "tls", "none"):
        return _check(
            "email", "FAIL", "NURAMESH_SMTP_SECURITY is not one of starttls, tls, none",
            "set NURAMESH_SMTP_SECURITY to starttls or tls",
        )  # fmt: skip
    if security == "none" and host not in ("localhost", "127.0.0.1", "::1"):
        return _check(
            "email", "FAIL", "NURAMESH_SMTP_SECURITY=none sends mail in clear to a remote host",
            "use starttls or tls; none is for a loopback host only",
        )  # fmt: skip
    return _check("email", "PASS", "email settings are complete (not tested by sending)")


SMTP_PORTS = {"starttls": 587, "tls": 465, "none": 25}
CONNECT_TIMEOUT = 5.0


def check_email_connect(network: bool) -> dict[str, str]:
    """TCP connect (and a TLS handshake for `tls`) to the mail server; nothing is sent. Runs only
    with `--network-checks`, and only through the guarded connector (private hosts refused)."""
    host = os.environ.get("NURAMESH_SMTP_HOST")
    if not network:
        return _check(
            "email_connect", "SKIPPED", "mail server connection not tested",
            "not run: add --network-checks to test the mail server connection",
        )  # fmt: skip
    if not host:
        return _check(
            "email_connect", "SKIPPED", "mail server connection not tested",
            "not run: NURAMESH_SMTP_HOST is not set",
        )  # fmt: skip
    security = os.environ.get("NURAMESH_SMTP_SECURITY", "starttls")
    if security == "none" and host in ("localhost", "127.0.0.1", "::1"):
        return _check(
            "email_connect", "SKIPPED", "loopback mail host not probed",
            "not run: doctor does not probe a mail server on this machine",
        )  # fmt: skip
    raw = os.environ.get("NURAMESH_SMTP_PORT", "")
    port = int(raw) if raw.isdigit() and 1 <= int(raw) <= 65535 else SMTP_PORTS.get(security, 587)
    from nuramesh.engine import net_guard

    try:
        conn = net_guard.connect((host, port), CONNECT_TIMEOUT)
        try:
            if security == "tls":
                from nuramesh.core.tls import default_client_context

                default_client_context().wrap_socket(conn, server_hostname=host).close()
        finally:
            conn.close()
    except net_guard.PrivateNetworkError:
        return _check(
            "email_connect", "FAIL", "the mail host is a private or loopback address: not probed",
            "doctor does not probe a mail server on your own network; check it with your mail tool",
        )  # fmt: skip
    except (OSError, ValueError):  # includes ssl.SSLError; the message may name the host
        return _check(
            "email_connect", "FAIL", f"could not connect to the mail server on port {port}",
            "check NURAMESH_SMTP_HOST, NURAMESH_SMTP_PORT, NURAMESH_SMTP_SECURITY and the firewall",
        )  # fmt: skip
    return _check("email_connect", "PASS", f"connected to the mail server on port {port}")


def check_tls_proxy() -> dict[str, str]:
    """Reads environment variables and checks that a named CA file exists; connects nowhere."""
    for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        given = os.environ.get(name)
        if given and not os.path.isfile(given):
            return _check(
                "tls_proxy", "FAIL", f"{name} names a file that does not exist",
                f"point {name} at your CA bundle, or unset it",
            )  # fmt: skip
    proxied = any(
        os.environ.get(n) for n in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")
    )
    if proxied:
        return _check("tls_proxy", "PASS", "an outbound proxy is set (its address is not shown)")
    return _check("tls_proxy", "PASS", "no proxy and no custom CA bundle set")


def check_disk() -> dict[str, str]:
    free = free_bytes()
    mib = free // 1024**2
    if free < FAIL_FREE:
        return _check(
            "disk", "FAIL", f"only {mib} MiB free here (needs 100 MiB)",
            "free disk space, or run from another directory",
        )  # fmt: skip
    if free < WARN_FREE:
        return _check(
            "disk", "WARN", f"only {mib} MiB free here (1 GiB is comfortable)",
            "free disk space before long runs",
        )  # fmt: skip
    return _check("disk", "PASS", f"{mib} MiB free here")


def check_extras() -> dict[str, str]:
    missing = [name for name, module in EXTRAS.items() if not module_present(module)]
    if not missing:
        return _check("extras", "PASS", "all extras are installed: " + ", ".join(EXTRAS))
    return _check(
        "extras", "WARN", "extras not installed: " + ", ".join(missing),
        "install what you use, for example " + " ".join(f"`uv sync --extra {m}`" for m in missing),
    )  # fmt: skip


def check_price_list(
    shipped: ShippedList | None = None, today: date | None = None
) -> dict[str, str]:
    """The shipped default price list loads and no key's newest row is stale (spec default-prices
    6.4). Reads the package file only: no network. An invalid or missing file is a FAIL."""
    try:
        listed = shipped if shipped is not None else load_shipped()
    except ShippedPriceError as exc:
        return _check(
            "price_list", "FAIL", f"shipped price list invalid: {exc}",
            "reinstall nuramesh; nothing is guessed, so calls to shipped models stay blocked",
        )  # fmt: skip
    now = today if today is not None else today_utc()
    keys = {(r.price.provider, r.price.model) for r in listed.rows}
    ages = [listed.age_days(p, m, now) or 0 for p, m in keys if listed.stale(p, m, now)]
    if ages:
        return _check(
            "price_list", "WARN", f"shipped price list is {max(ages)} days old",
            f"prices older than {STALE_AFTER_DAYS} days are still used; upgrade nuramesh or set "
            "your own with `nuramesh models price`",
        )  # fmt: skip
    return _check("price_list", "PASS", f"shipped price list {listed.list_version} is current")


def run_checks(host: str, port: int, network: bool = False) -> list[dict[str, str]]:
    """Every check, in a fixed order."""
    return [
        check_python(),
        check_port(host, port),
        check_database(network),
        check_auth_key(),
        check_docker(),
        check_sandbox(),
        check_extras(),
        check_email(),
        check_email_connect(network),
        check_tls_proxy(),
        check_disk(),
        check_price_list(),
    ]


def cmd_doctor(args: argparse.Namespace) -> int:
    """Print the checks; exit 1 when any failed."""
    checks = run_checks(args.host, args.port, getattr(args, "network_checks", False))
    counts = {
        s: sum(1 for c in checks if c["status"] == s) for s in ("PASS", "WARN", "FAIL", "SKIPPED")
    }
    ok = counts["FAIL"] == 0
    if getattr(args, "output", None) == "json":
        summary = {
            "pass": counts["PASS"],
            "warn": counts["WARN"],
            "fail": counts["FAIL"],
            "skipped": counts["SKIPPED"],
        }
        print(json.dumps({"ok": ok, "summary": summary, "checks": checks}, indent=2))
        return OK if ok else FAILED
    for c in checks:
        print(f"{c['status']} {c['id']}: {c['message']}")
        if c["hint"]:
            print(f"     fix: {c['hint']}")
    print(
        f"{counts['PASS']} passed, {counts['WARN']} warnings, {counts['FAIL']} failed, "
        f"{counts['SKIPPED']} not run"
    )
    return OK if ok else FAILED


def add_parsers(sub: Any) -> None:
    """Register `doctor`."""
    doc = sub.add_parser(
        "doctor",
        help="check this machine: Python, port, database, auth key, container runtime, extras, "
        "email, TLS and proxy, disk",
        description="Prints PASS, WARN or FAIL per check with a fix hint; exit 1 when a check "
        "failed. Changes nothing on this machine.",
    )
    doc.add_argument("--host", default="127.0.0.1", help="the address `serve` would bind")
    doc.add_argument("--port", type=int, default=DEFAULT_PORT, help="the port `serve` would use")
    doc.add_argument(
        "--network-checks", action="store_true",
        help="also test the database connection (when NURAMESH_DATABASE_URL is set) and the mail "
        "server connection (TCP connect only for starttls and none; TLS handshake for tls); "
        "without this flag doctor connects nowhere. The TLS and proxy check only reads "
        "environment variables and never connects",
    )  # fmt: skip
    doc.set_defaults(func=cmd_doctor)
