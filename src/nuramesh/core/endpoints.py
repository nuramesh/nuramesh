"""Endpoint rules of the `openai-compatible` provider that core types need (pure `urllib` logic).

The full URL rules (and their fixed texts) live in `engine.openai_compatible`; this module holds
the small, shared pieces that prices, catalog entries and the resolver also use: which endpoint is
a literal loopback address, how an endpoint is normalised and compared, the connection
fingerprint, and the residency rule for a remote endpoint (ND45).
"""

import hashlib
import json
from urllib.parse import urlsplit

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "[::1]"})
"""The only hosts `http` is allowed to (the name `localhost` is never accepted)."""

FREE_SOURCE = "declared free (local)"
"""The only source a zero price may carry."""

REMOTE_RESIDENCY = "openai-compatible: a remote endpoint can only declare global processing"


def _split(endpoint: str):  # type: ignore[no-untyped-def]
    try:
        return urlsplit(endpoint)
    except ValueError:
        return None


def host_of(endpoint: str) -> str | None:
    """The lower-case host of `endpoint` as written (`[::1]` keeps its brackets), or None."""
    parts = _split(endpoint)
    if parts is None or not parts.hostname:
        return None
    host = parts.hostname.lower()
    return f"[{host}]" if ":" in host else host


def is_loopback(endpoint: str) -> bool:
    """True when the host is exactly the literal `127.0.0.1` or `[::1]` (either scheme)."""
    parts = _split(endpoint)
    if parts is None or parts.scheme not in ("http", "https"):
        return False
    return host_of(endpoint) in LOOPBACK_HOSTS


def is_remote(endpoint: str) -> bool:
    """True when the host is not a literal loopback address."""
    return not is_loopback(endpoint)


def normalise(endpoint: str) -> str:
    """Trailing `/` removed, scheme and host lower-cased, the path kept as given (case included)."""
    parts = _split(endpoint)
    if parts is None or not parts.scheme or not parts.netloc:
        return endpoint
    host = host_of(endpoint) or ""
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{parts.scheme.lower()}://{host}{port}{parts.path.rstrip('/')}"


def residency_problem(endpoint: str, processing: str) -> str | None:
    """The fixed text when a remote endpoint declares anything but `global` processing, else None.

    The adapter cannot verify where a vendor processes data, so by ND45 a remote endpoint takes
    only `synthetic` data; a loopback endpoint may declare any scope the organization permits.
    """
    if is_remote(endpoint) and processing != "global":
        return REMOTE_RESIDENCY
    return None


def fingerprint(endpoint: str, auth: str, key: str | None, endpoint_id: str | None) -> str:
    """The connection fingerprint an approval is recorded against (spec 2.4): the lower-case hex
    `sha256` of the canonical JSON of auth, normalised endpoint, endpoint id and key *name*."""
    canonical = json.dumps(
        {"auth": auth, "endpoint": normalise(endpoint), "endpoint_id": endpoint_id, "key": key},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
