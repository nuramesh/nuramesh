"""Which engine runs durable runs (R1.20 PR B, ND347 section 3, rules 1 to 6).

`NURAMESH_RUN_ENGINE` is `auto` (default), `temporal` or `light`:

1. `auto` with `NURAMESH_TEMPORAL_ADDRESS` set: `temporal`; without it: `light` (self-hosted).
2. An explicit `light` or `temporal` wins over `auto`; `light` with an address set (self-hosted)
   is `light` and the address is ignored; `temporal` without an address is refused.
3. Hosted (`NURAMESH_HOSTED=1`): `temporal` only. `light`, a missing address or the plaintext
   allowance is refused before a port is bound.

A refusal is `EngineSelectionError` with a plain one-line message (the CLI exits 2). Pure: it reads
a mapping, never the network.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from nuramesh.env_bool import EnvBoolError, env_bool
from nuramesh.hosted import hosted_from_env

ENGINE_ENV: Final = "NURAMESH_RUN_ENGINE"
ADDRESS_ENV: Final = "NURAMESH_TEMPORAL_ADDRESS"
PLAINTEXT_ENV: Final = "NURAMESH_TEMPORAL_ALLOW_PLAINTEXT"
LIGHT: Final = "light"
TEMPORAL: Final = "temporal"
_CHOICES: Final = ("auto", LIGHT, TEMPORAL)


class EngineSelectionError(ValueError):
    """The engine settings are refused; the message says what to change."""


@dataclass(frozen=True)
class EngineChoice:
    engine: str
    hosted: bool
    note: str = ""
    """A line to log (for example that the Temporal address is ignored), or empty."""


def select_engine(environ: Mapping[str, str]) -> EngineChoice:
    """The engine for this deployment, or `EngineSelectionError`."""
    requested = environ.get(ENGINE_ENV, "").strip().lower() or "auto"
    if requested not in _CHOICES:
        raise EngineSelectionError(f"{ENGINE_ENV} must be one of: {', '.join(_CHOICES)}")
    address = environ.get(ADDRESS_ENV, "").strip()
    hosted = hosted_from_env(environ)
    if hosted:
        if requested == LIGHT:
            raise EngineSelectionError(
                f"a hosted deployment runs the Temporal engine only; {ENGINE_ENV}=light is refused"
            )
        if not address:
            raise EngineSelectionError(
                f"a hosted deployment needs {ADDRESS_ENV} (the Temporal engine is required)"
            )
        try:
            plaintext = env_bool(PLAINTEXT_ENV, environ)
        except EnvBoolError as exc:
            raise EngineSelectionError(str(exc)) from None
        if plaintext:
            raise EngineSelectionError(
                f"a hosted deployment may not use {PLAINTEXT_ENV}; use a TLS Temporal address"
            )
        return EngineChoice(TEMPORAL, True)
    if requested == TEMPORAL:
        if not address:
            raise EngineSelectionError(f"{ENGINE_ENV}=temporal needs {ADDRESS_ENV}")
        return EngineChoice(TEMPORAL, False)
    if requested == LIGHT:
        note = f"{ADDRESS_ENV} is ignored: {ENGINE_ENV}=light" if address else ""
        return EngineChoice(LIGHT, False, note)
    return EngineChoice(TEMPORAL if address else LIGHT, False)


def refuse_light_when_hosted(environ: Mapping[str, str] | None = None) -> None:
    """S12, defence in depth: the light engine's constructors call this. A hosted deployment runs
    the Temporal engine only, so building a light starter or runner there is an error."""
    if hosted_from_env(environ):
        raise EngineSelectionError(
            "a hosted deployment runs the Temporal engine only; the light engine is refused"
        )
