"""Transport security for the Temporal connection (R1.30 review 2, m4, owner yes).

Anyone who can reach the Temporal frontend can cancel or terminate a run's workflow, so a
frontend that is not on this machine is only reached over TLS and with credentials:

* **Loopback** (`localhost`, `127.0.0.0/8`, `::1`) is allowed without TLS (the dev server).
* **Any other address** needs `NURAMESH_TEMPORAL_TLS=1` AND credentials: a client certificate and
  key (`NURAMESH_TEMPORAL_TLS_CERT_FILE` + `NURAMESH_TEMPORAL_TLS_KEY_FILE`, mutual TLS) or an API
  key (`NURAMESH_TEMPORAL_API_KEY`, or `NURAMESH_TEMPORAL_API_KEY_FILE`). Optional:
  `NURAMESH_TEMPORAL_TLS_CA_FILE` (a private CA) and `NURAMESH_TEMPORAL_TLS_SERVER_NAME`.

**Dev stack allowance.** `NURAMESH_TEMPORAL_ALLOW_PLAINTEXT=1` lets a non-loopback address be
reached without TLS (the Compose stack's internal-only Temporal dev server, evaluation only). It
never combines with an API key, it is honoured only in a process that has first written the ledger
entry `network.temporal_plaintext_allowed` (`record_plaintext_allowance`), and without that entry
the connection is refused.

It fails closed: anything missing, unreadable, not private or malformed raises
`TemporalSecurityError` before any connection is tried. Messages name the setting, never a value,
a path or a file's content, and the API key is kept out of `repr`.
"""

import ipaddress
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field

from temporalio.service import TLSConfig

from nuramesh.core.ledger import EventType
from nuramesh.env_bool import EnvBoolError, env_bool
from nuramesh.stores.base import EventDraft, Store

TLS_FLAG = "NURAMESH_TEMPORAL_TLS"
CA_FILE = "NURAMESH_TEMPORAL_TLS_CA_FILE"
CERT_FILE = "NURAMESH_TEMPORAL_TLS_CERT_FILE"
KEY_FILE = "NURAMESH_TEMPORAL_TLS_KEY_FILE"
SERVER_NAME = "NURAMESH_TEMPORAL_TLS_SERVER_NAME"
API_KEY = "NURAMESH_TEMPORAL_API_KEY"
API_KEY_FILE = "NURAMESH_TEMPORAL_API_KEY_FILE"
ALLOW_PLAINTEXT = "NURAMESH_TEMPORAL_ALLOW_PLAINTEXT"
MAX_FILE = 65536
MAX_API_KEY = 4096


class TemporalSecurityError(RuntimeError):
    """The Temporal transport settings are missing or invalid; the text names no value."""


@dataclass(frozen=True)
class TemporalSecurity:
    """What `Client.connect` is given: a TLS config (or None for plain loopback) and an API key."""

    tls: TLSConfig | None
    api_key: str | None = field(default=None, repr=False)


_HOST = re.compile(r"[A-Za-z0-9._-]+")
_V6 = re.compile(r"[0-9A-Fa-f:.]+")


def parse_address(address: str) -> tuple[str, int | None] | None:
    """`(host, port)` of an address written exactly `host`, `host:port`, `[v6]` or `[v6]:port`
    (and the bare `::1`), else None. The port, if present, is 1 to 65535.

    The SDK builds `http://{address}` (or uses a text with `://` as a URL), so anything that is not
    that plain shape can name a different host than the one checked: `localhost:7233@remote`
    (userinfo), `http://host:port` (a scheme), a path, a space, an empty or non-numeric port. Those
    are all None, never guessed at."""
    if address == "::1":
        return address, None
    if address.startswith("["):
        host, sep, rest = address[1:].partition("]")
        if not sep or not _V6.fullmatch(host) or ":" not in host:
            return None
        if rest == "":
            return host, None
        if not rest.startswith(":"):
            return None
        port_text = rest[1:]
    elif ":" in address:
        host, _, port_text = address.partition(":")
        if ":" in port_text:  # an unbracketed v6 other than ::1
            return None
    else:
        host, port_text = address, ""
        if not _HOST.fullmatch(host):
            return None
        return host, None
    if not _HOST.fullmatch(host) and not address.startswith("["):
        return None
    if not (port_text.isascii() and port_text.isdecimal() and port_text == port_text.lstrip("0")):
        return None
    port = int(port_text)
    if not 0 < port <= 65535:
        return None
    return host, port


def is_loopback(address: str) -> bool:
    """True only for a well-formed `host:port` / `[v6]:port` whose host is the exact name
    `localhost`, an address in 127.0.0.0/8 or `::1`. A name is never resolved, so a lookalike
    (`LOCALHOST`, `localhost.`, `127.1`) is not loopback; a malformed address is not either."""
    parsed = parse_address(address)
    if parsed is None:
        return False
    host = parsed[0]
    if host == "localhost":
        return True
    try:
        found = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(found, ipaddress.IPv6Address) and found.ipv4_mapped is not None:
        return False  # a mapped address is not `::1`; Python 3.13 would unwrap it, this does not
    return found.is_loopback


def _loopback_hint(address: str) -> str:
    parsed = parse_address(address)
    if parsed is not None and parsed[0] != "localhost" and parsed[0].lower() == "localhost":
        return " (only the lowercase name `localhost` counts as loopback)"
    return ""


_recorded = False  # set only by `record_plaintext_allowance`, once per process


def plaintext_requested(env: Mapping[str, str]) -> bool:
    """Whether `NURAMESH_TEMPORAL_ALLOW_PLAINTEXT` asks for the dev allowance (1 or 0)."""
    try:
        return env_bool(ALLOW_PLAINTEXT, env)
    except EnvBoolError as exc:
        raise TemporalSecurityError(str(exc)) from None


def record_plaintext_allowance(
    store: Store, org_id: str, env: Mapping[str, str], address: str
) -> bool:
    """Write the ledger entry for the dev allowance (once per process) and arm it. True if the
    allowance is in force. Does nothing, and writes nothing, when it was not asked for, when
    `address` is loopback, when TLS is on, or when `security_for` would refuse the address (the
    refusal is raised). Call it in every process that connects to Temporal (worker, API server)
    before the first connection."""
    global _recorded
    if not plaintext_requested(env):
        return False
    security_for(address, env, allowance_recorded=True)  # a refused address writes no entry
    if is_loopback(address) or _flag(env):
        return False  # the allowance is not used here: the ledger must not say it was
    if not _recorded:
        with store.transaction() as uow:
            uow.append(
                EventDraft(
                    stream=f"org:{org_id}",
                    type=EventType.NETWORK_TEMPORAL_PLAINTEXT_ALLOWED,
                    actor="system",
                    payload={"setting": ALLOW_PLAINTEXT},
                )
            )
        _recorded = True
    return True


def _flag(env: Mapping[str, str]) -> bool:
    try:
        return env_bool(TLS_FLAG, env)
    except EnvBoolError as exc:
        raise TemporalSecurityError(str(exc)) from None


def _read(name: str, path: str, *, private: bool, marker: str | None) -> bytes:
    """A credential file read safely: no symlink, a regular file the process owns, not readable
    by others when it holds a secret, bounded, and (for PEM files) with the expected header."""
    bad = TemporalSecurityError(f"{name} is missing, unreadable, not private or malformed")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise bad from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE:
            raise bad
        if info.st_uid != os.geteuid() or (private and info.st_mode & 0o077):
            raise bad
        data = os.read(fd, MAX_FILE + 1)
    except OSError:
        raise bad from None
    finally:
        os.close(fd)
    if len(data) > MAX_FILE or (marker is not None and marker.encode() not in data):
        raise bad
    return data


def _api_key(env: Mapping[str, str]) -> str | None:
    inline, path = env.get(API_KEY, ""), env.get(API_KEY_FILE, "").strip()
    if inline.strip() and path:
        raise TemporalSecurityError(f"set only one of {API_KEY} and {API_KEY_FILE}")
    if path:
        key = _read(API_KEY_FILE, path, private=True, marker=None).decode("ascii", "replace")
    elif inline.strip():
        key = inline
    else:
        return None
    key = key.strip()
    if not key or len(key) > MAX_API_KEY or any(not 33 <= ord(c) <= 126 for c in key):
        raise TemporalSecurityError(
            f"{API_KEY} is empty, too long or has a space or control character"
        )
    return key


def security_for(
    address: str, env: Mapping[str, str], *, allowance_recorded: bool | None = None
) -> TemporalSecurity:
    """The transport settings for `address` from `env`, or `TemporalSecurityError`."""
    if parse_address(address) is None:
        raise TemporalSecurityError(
            "the Temporal address must be exactly host:port or [v6]:port "
            "(no scheme, user, path or space)"
        )
    loopback = is_loopback(address)
    tls_on = _flag(env)
    api_key = _api_key(env)
    cert, key = env.get(CERT_FILE, "").strip(), env.get(KEY_FILE, "").strip()
    ca, name = env.get(CA_FILE, "").strip(), env.get(SERVER_NAME, "").strip()
    if bool(cert) != bool(key):
        raise TemporalSecurityError(f"{CERT_FILE} and {KEY_FILE} must both be set")
    if not tls_on:
        if not loopback and plaintext_requested(env):
            if api_key:
                raise TemporalSecurityError(f"{API_KEY} needs TLS ({TLS_FLAG}=1)")
            if not (_recorded if allowance_recorded is None else allowance_recorded):
                raise TemporalSecurityError(
                    f"{ALLOW_PLAINTEXT} is honoured only after the ledger entry is written"
                )
            return TemporalSecurity(tls=None)
        if not loopback:
            raise TemporalSecurityError(
                f"the Temporal address is not loopback: it needs TLS ({TLS_FLAG}=1) "
                "and credentials (a client certificate or an API key)" + _loopback_hint(address)
            )
        if api_key:
            raise TemporalSecurityError(f"{API_KEY} needs TLS ({TLS_FLAG}=1)")
        return TemporalSecurity(tls=None)
    if not loopback and not (cert or api_key):
        raise TemporalSecurityError(
            "the Temporal address is not loopback: it needs credentials "
            f"({CERT_FILE} with {KEY_FILE}, or {API_KEY})" + _loopback_hint(address)
        )
    tls = TLSConfig(
        server_root_ca_cert=_read(CA_FILE, ca, private=False, marker="-----BEGIN ") if ca else None,
        client_cert=_read(CERT_FILE, cert, private=False, marker="-----BEGIN ") if cert else None,
        client_private_key=_read(KEY_FILE, key, private=True, marker="PRIVATE KEY-----")
        if key
        else None,
        domain=name or None,
    )
    return TemporalSecurity(tls=tls, api_key=api_key)


def security_from_env(address: str) -> TemporalSecurity:
    """`security_for` with the process environment."""
    return security_for(address, os.environ)
