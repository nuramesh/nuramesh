"""The one grammar for every `NURAMESH_*` on/off setting (ND423, #896 slice 2).

On is `1`, `true`, `yes` or `on`; off is `0`, `false`, `no` or `off`; any case, surrounding
whitespace ignored. Unset means the setting's default, and so does a variable that is set but
empty or only whitespace (empty = unset, design decision 2026-11-13). Any other text is a
configuration error that names the variable and never echoes the value: a typo must not silently
turn a safety flag on or off. Stdlib only, so any
layer (including `nuramesh.core`) may import it."""

import os
from collections.abc import Mapping
from typing import Final

TRUE_WORDS: Final = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS: Final = frozenset({"0", "false", "no", "off"})


class EnvBoolError(ValueError):
    """A boolean setting holds a value that is not one of the eight words. One line, names the
    variable only."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"{name} must be 1, true, yes or on, or 0, false, no or off")


def parse_bool(name: str, raw: str) -> bool:
    """The value of a non-empty variable; `EnvBoolError(name)` for any other text (the caller treats
    empty as unset)."""
    word = raw.strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise EnvBoolError(name)


def env_bool(name: str, env: Mapping[str, str] | None = None, *, default: bool = False) -> bool:
    """`default` when `name` is unset or empty, else `parse_bool` of its value."""
    raw = (os.environ if env is None else env).get(name)
    if raw is None or not raw.strip():
        return default
    return parse_bool(name, raw)


# Every boolean setting that stops the process at start when it is malformed. The `NURAMESH_OTEL_*`
# booleans use the same grammar but never stop the server (the exporter is disabled instead), so
# they are not listed here. `tests/test_env_bool.py` pins this tuple against the docs page.
STARTUP_BOOLEANS: Final = (
    "NURAMESH_HOSTED",
    "NURAMESH_REQUIRE_HOSTED",
    "NURAMESH_CUSTOMER_DATA_ENABLED",
    "NURAMESH_TEMPORAL_TLS",
    "NURAMESH_TEMPORAL_ALLOW_PLAINTEXT",
    "NURAMESH_ALLOW_PRIVATE_NETWORK",
    "NURAMESH_REQUIRE_DATABASE",
    "NURAMESH_OPERATOR",
    "NURAMESH_KEYS_DPA_PUBLISHED",
    "NURAMESH_SMTP_ALLOW_INSECURE",
)


def startup_problem(env: Mapping[str, str] | None = None) -> str | None:
    """One line naming the first malformed boolean in `STARTUP_BOOLEANS`, or None."""
    for name in STARTUP_BOOLEANS:
        try:
            env_bool(name, env)
        except EnvBoolError as exc:
            return str(exc)
    return None
