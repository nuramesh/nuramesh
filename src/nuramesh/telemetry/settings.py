"""Exporter settings from the environment (E3 spec sections 1 and 10; case E3-16).

Only `NURAMESH_OTEL_*` variables are read. The standard `OTEL_*` variables are ignored on
purpose: one set for another tool must never start exporting NuraMesh data. A refusal is an
`OtelSettingsError` holding a closed code and, for a bad setting, the variable name. Neither the
message nor any attribute ever holds a value (a header value is a secret).
"""

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from nuramesh.env_bool import EnvBoolError, parse_bool
from nuramesh.paths import home_path
from nuramesh.secret_env import SecretEnvError, read_secret
from nuramesh.telemetry.guard import is_safe_string

PREFIX = "NURAMESH_OTEL_"
ENDPOINT_CODES = (
    "bad_scheme",
    "insecure_endpoint",
    "credentials_in_url",
    "query_or_fragment",
    "empty_endpoint",
)
CODES = (*ENDPOINT_CODES, "bad_setting")
SIGNALS = ("traces", "metrics")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class OtelSettingsError(Exception):
    """A setting the exporter refuses. `code` is one of `CODES`; `variable` names the setting for
    `bad_setting`. The text holds no value."""

    def __init__(self, code: str, variable: str | None = None) -> None:
        self.code = code
        self.variable = variable
        super().__init__(code if variable is None else f"{code}: {variable}")


@dataclass(frozen=True)
class OtelSettings:
    """A valid, enabled configuration. `repr` hides the headers (they are a secret)."""

    endpoint: str
    headers: tuple[tuple[str, str], ...]
    allow_insecure: bool
    signals: frozenset[str]
    sample_ratio: Decimal
    person_ids: str
    pseudonym_key_file: Path
    event_hash: bool
    cost_metric: bool
    genai_attrs: bool
    batch_size: int
    poll_seconds: int
    export_timeout_seconds: int
    max_lag_events: int
    state_file: Path
    deployment: str | None

    def __repr__(self) -> str:
        return "OtelSettings(<redacted>)"

    __str__ = __repr__


def key_dir(env: Mapping[str, str]) -> Path:
    """`NURAMESH_KEY_DIR`, else `~/.nuramesh/keys` (the same place as the content key ring)."""
    configured = env.get("NURAMESH_KEY_DIR")
    return Path(configured) if configured else home_path("keys")


def pseudonym_key_path(env: Mapping[str, str] | None = None) -> Path:
    """Where the pseudonym key lives (`NURAMESH_OTEL_PSEUDONYM_KEY_FILE` or the key dir)."""
    source = os.environ if env is None else env
    configured = source.get(PREFIX + "PSEUDONYM_KEY_FILE")
    return Path(configured) if configured else key_dir(source) / "otel-pseudonym.key"


def check_endpoint(endpoint: str, *, allow_insecure: bool) -> str:
    """The endpoint with trailing slashes removed, or an `OtelSettingsError` with the code."""
    text = endpoint.strip()
    if not text:
        raise OtelSettingsError("empty_endpoint")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise OtelSettingsError("bad_setting", PREFIX + "ENDPOINT") from None
    if parts.scheme not in ("http", "https"):
        raise OtelSettingsError("bad_scheme")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise OtelSettingsError("credentials_in_url")
    if parts.query or parts.fragment or "?" in text or "#" in text:
        raise OtelSettingsError("query_or_fragment")
    host = parts.hostname
    if not host or port == 0:
        raise OtelSettingsError("bad_setting", PREFIX + "ENDPOINT")
    if parts.scheme == "http" and not allow_insecure and not _is_loopback(host):
        raise OtelSettingsError("insecure_endpoint")
    return text.rstrip("/")


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(PREFIX + name)
    if raw is None or not raw.strip():
        return default
    try:
        return parse_bool(PREFIX + name, raw)
    except EnvBoolError:
        raise OtelSettingsError("bad_setting", PREFIX + name) from None


def _int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(PREFIX + name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise OtelSettingsError("bad_setting", PREFIX + name) from None
    if not low <= value <= high:
        raise OtelSettingsError("bad_setting", PREFIX + name)
    return value


def parse_headers(raw: str) -> tuple[tuple[str, str], ...]:
    """`key=value,key=value`. Names are `[A-Za-z0-9-]+`; values hold no control characters."""
    variable = PREFIX + "HEADERS"
    pairs: list[tuple[str, str]] = []
    for piece in raw.split(","):
        name, separator, value = piece.partition("=")
        name, value = name.strip(), value.strip()
        if not separator or not _HEADER_NAME.fullmatch(name) or not value or _CONTROL.search(value):
            raise OtelSettingsError("bad_setting", variable)
        pairs.append((name, value))
    return tuple(pairs)


def _ratio(raw: str) -> Decimal:
    variable = PREFIX + "SAMPLE_RATIO"
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        raise OtelSettingsError("bad_setting", variable) from None
    if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
        raise OtelSettingsError("bad_setting", variable)
    return value


def _signals(raw: str) -> frozenset[str]:
    names = [piece.strip() for piece in raw.split(",")]
    if not names or any(name not in SIGNALS for name in names):
        raise OtelSettingsError("bad_setting", PREFIX + "SIGNALS")
    return frozenset(names)


def load_settings(env: Mapping[str, str] | None = None) -> OtelSettings | None:
    """`None` when `NURAMESH_OTEL_ENDPOINT` is not set (the exporter does not exist). Otherwise
    the settings, or an `OtelSettingsError`. `OTEL_*` variables are never read."""
    source = os.environ if env is None else env
    endpoint_raw = source.get(PREFIX + "ENDPOINT")
    if endpoint_raw is None:
        return None
    allow_insecure = _bool(source, "ALLOW_INSECURE", False)
    endpoint = check_endpoint(endpoint_raw, allow_insecure=allow_insecure)
    try:
        headers_raw = read_secret(PREFIX + "HEADERS", source)
    except SecretEnvError:
        raise OtelSettingsError("bad_setting", PREFIX + "HEADERS") from None
    person_ids = source.get(PREFIX + "PERSON_IDS", "none").strip()
    if person_ids not in ("none", "pseudonym"):
        raise OtelSettingsError("bad_setting", PREFIX + "PERSON_IDS")
    ratio_raw = source.get(PREFIX + "SAMPLE_RATIO")
    signals_raw = source.get(PREFIX + "SIGNALS")
    deployment = source.get(PREFIX + "DEPLOYMENT")
    if deployment is not None and not is_safe_string(deployment):
        raise OtelSettingsError("bad_setting", PREFIX + "DEPLOYMENT")
    state = source.get(PREFIX + "STATE_FILE")
    return OtelSettings(
        endpoint=endpoint,
        headers=() if headers_raw is None else parse_headers(headers_raw),
        allow_insecure=allow_insecure,
        signals=frozenset(SIGNALS) if signals_raw is None else _signals(signals_raw),
        sample_ratio=Decimal(1) if ratio_raw is None else _ratio(ratio_raw),
        person_ids=person_ids,
        pseudonym_key_file=pseudonym_key_path(source),
        event_hash=_bool(source, "EVENT_HASH", True),
        cost_metric=_bool(source, "COST_METRIC", False),
        genai_attrs=_bool(source, "GENAI_ATTRS", False),
        batch_size=_int(source, "BATCH_SIZE", 256, 1, 4096),
        poll_seconds=_int(source, "POLL_SECONDS", 2, 1, 300),
        export_timeout_seconds=_int(source, "EXPORT_TIMEOUT_SECONDS", 10, 1, 120),
        max_lag_events=_int(source, "MAX_LAG_EVENTS", 100000, 1, 10**9),
        state_file=Path(state) if state else key_dir(source) / "otel-cursors.json",
        deployment=deployment,
    )
