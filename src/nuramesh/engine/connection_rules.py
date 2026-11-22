"""Which providers can be connected, with which credential type, at which endpoint (R1.14b PR 2).

The keyless Azure providers authenticate with Entra (`auth "entra"`, an endpoint they own); the
Anthropic provider uses the server's key file (`auth "key_file"`, one fixed endpoint). The pair of
provider and `auth` is closed. The checks return fixed text and never quote their input.
"""

from nuramesh.core.models import ENDPOINT_NOT_ALLOWED
from nuramesh.core.org import OPENAI_COMPATIBLE
from nuramesh.engine import openai_compatible as oac
from nuramesh.engine.anthropic import ENDPOINT, KEY_FILE_PROVIDERS
from nuramesh.engine.anthropic import connection_problem as key_file_connection_problem
from nuramesh.engine.azure_openai import KEYLESS_PROVIDERS
from nuramesh.engine.azure_openai import connection_problem as keyless_connection_problem

SUPPORTED_PROVIDERS: tuple[str, ...] = (
    *KEYLESS_PROVIDERS,
    *sorted(KEY_FILE_PROVIDERS),
    OPENAI_COMPATIBLE,
)


def auth_of(provider: str, requested: str | None = None) -> str:
    """The `auth` a connection of `provider` carries. `openai-compatible` carries its own
    (`requested`: `none` or `key`); the others have one fixed credential type."""
    if provider == OPENAI_COMPATIBLE:
        return requested or ""
    return "key_file" if provider in KEY_FILE_PROVIDERS else "entra"


def default_endpoint(provider: str) -> str | None:
    """The fixed endpoint of a key-file provider, or None when the caller names the endpoint."""
    return ENDPOINT if provider in KEY_FILE_PROVIDERS else None


def history_problem(
    provider: str,
    endpoint: str,
    *,
    endpoint_id: str | None = None,
    auth: str | None = None,
    key: str | None = None,
) -> str | None:
    """Every check of `connection_problem` except the hosted-mode refusal (Q4, ND311: option B).

    This is the validity the connection HISTORY read judges a row by, so the audit view stays
    readable on a hosted server. It is never a licence to USE a connection: the resolver, the
    connection test and the adapters all call `connection_problem`, which adds the hosted refusal.
    A test keeps it that way (no file on the use path mentions this function)."""
    if provider == OPENAI_COMPATIBLE:
        return oac.connection_problem(provider, endpoint, auth or "", key, endpoint_id)
    if provider in SUPPORTED_PROVIDERS and endpoint_id is not None:
        return ENDPOINT_NOT_ALLOWED
    if provider in KEY_FILE_PROVIDERS:
        return key_file_connection_problem(provider, endpoint)
    return keyless_connection_problem(provider, endpoint)


def connection_problem(
    provider: str,
    endpoint: str,
    *,
    endpoint_id: str | None = None,
    auth: str | None = None,
    key: str | None = None,
) -> str | None:
    """Why a connection to `provider` at `endpoint` may not be USED, or None if it is fine.

    The order of spec openai-compatible 2.1: the hosted refusal, then `history_problem` (the
    provider is supported, then the endpoint id rule, then the URL and auth rules)."""
    if provider == OPENAI_COMPATIBLE:
        hosted = oac.hosted_problem()
        if hosted is not None:
            return hosted
    return history_problem(provider, endpoint, endpoint_id=endpoint_id, auth=auth, key=key)
