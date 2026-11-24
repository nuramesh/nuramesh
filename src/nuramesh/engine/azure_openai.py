"""The Azure OpenAI (Foundry) model provider: keyless, Canada East, no new dependency by default
(R1.14b, ND26, ND43, ND45).

`AzureOpenAIProvider.complete` sends one chat completion to the Azure OpenAI v1 API of **one
resource** and maps the answer to a `ModelResponse` (text, finish reason, tokens including cached
input). Everything that can go wrong becomes `ProviderError` (not charged); error text never
quotes the request, the response or a token.

- **Keyless.** The bearer token comes from a `TokenProvider` (a function returning an Entra ID
  access token for `https://cognitiveservices.azure.com/.default`). `entra_token_provider()` builds
  the standard one from `azure-identity`'s `DefaultAzureCredential` (the optional `azure` extra:
  your `az login` locally, a managed identity in Azure). There is no API key anywhere: the Foundry
  account has local authentication disabled.
- **The token goes only to the resource.** The endpoint must be `https://<name>.openai.azure.com`
  (or `.cognitiveservices.azure.com`); redirects are refused, so the token can't be sent elsewhere.
- **Data residency (ND45).** The provider is given its processing scope (it comes from the
  catalog entry of the model, never from a default here: `global` for the GlobalStandard SKU,
  which may process prompts outside Canada) and the run's data class; the constructor refuses a
  combination the rule forbids (`core.residency`), so a `global` deployment can never be built for
  customer data.
- **The endpoint comes only from a connection** (ND92). Nothing here or in the CLI reads an
  endpoint environment variable; the engine builds a provider from the endpoint the resolver
  resolved (`engine.governance`).
- **`probe`** is the one place that knows the path of the model-listing operation: a body-less,
  keyless `GET` that never follows a redirect, used by the admin's test-connection action.
- **The transport is injectable** (`post`), so tests never touch the network.
"""

import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nuramesh.core.models import CacheSupport, ModelRequest, ModelResponse, ProviderError
from nuramesh.core.prices import MAX_TOKENS, TokenUsage
from nuramesh.core.residency import require_data_residency
from nuramesh.core.secrets_text import CredentialGetter, Redacted, reveal_secret
from nuramesh.engine import call_signal, net_guard

SCOPE = "https://cognitiveservices.azure.com/.default"
# The only endpoints the Entra token is ever sent to (the adapter and the connections share it).
ENDPOINT = re.compile(r"https://[a-z0-9][a-z0-9-]{0,62}\.(openai|cognitiveservices)\.azure\.com/?")
# Providers that authenticate with Entra ID, so a connection to them holds no secret.
KEYLESS_PROVIDERS = ("azure-openai", "azure-ai")
# the role the owner holds on the account of each provider, named in a 403 message
ROLE_HINT = {
    "azure-openai": "Cognitive Services OpenAI User",
    "azure-ai": "Cognitive Services User",
}
MAX_RESPONSE_BYTES = 8_000_000
_TOKEN = re.compile(r"[A-Za-z0-9._~+/=-]{10,16384}")  # a JWT: printable, no spaces or CR/LF
DEFAULT_TIMEOUT = 120.0


def connection_problem(provider: str, endpoint: str) -> str | None:
    """Why a keyless connection to `provider` at `endpoint` is refused, or None if it is fine.
    The messages are fixed text: the input is never quoted (it may be something pasted by mistake).
    """
    if provider not in KEYLESS_PROVIDERS:
        return "this provider needs a stored key, which is not available"
    if not ENDPOINT.fullmatch(endpoint):
        return (
            "the endpoint must be https://<name>.openai.azure.com (or .cognitiveservices.azure.com)"
        )
    return None


TokenProvider = Callable[[], str]


def _authorization(credential: Redacted | None) -> str | None:
    """The `Authorization` header value for a credential (the one place this adapter reveals it),
    or None when it is missing or not shaped like a token."""
    if not isinstance(credential, Redacted):
        return None
    token = reveal_secret(credential)
    return f"Bearer {token}" if _TOKEN.fullmatch(token) else None


ProbeTransport = Callable[[str, dict[str, str], float], int]
"""`(url, headers, timeout seconds) -> HTTP status`; raises OSError or TimeoutError when the
endpoint cannot be reached. Never follows a redirect and discards the body."""
PROBE_TIMEOUT = 10.0
PROBE_PATH = "/openai/v1/models"
"""The model-listing operation of the v1 API (a metadata read: no tokens, no spend)."""
Post = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def proxy_variables_set() -> list[str]:
    """The names (upper case, never the values) of the proxy variables set, in any case."""
    return [
        name for name in PROXY_VARIABLES if os.environ.get(name) or os.environ.get(name.lower())
    ]


def proxy_variable_set() -> bool:
    """True when any of HTTP_PROXY, HTTPS_PROXY, ALL_PROXY is set (any case, not empty)."""
    return bool(proxy_variables_set())


def entra_token_provider() -> TokenProvider:
    """A token provider from `DefaultAzureCredential` (needs `pip install 'nuramesh[azure]'`).

    The credential is built on first use, so a bad endpoint or a forbidden data class is refused
    before anything Azure-related is touched.
    """
    credential: Any = None
    built_proxied = False

    def token() -> str:
        nonlocal credential, built_proxied
        if credential is None or built_proxied != proxy_variable_set():
            try:
                from azure.identity import DefaultAzureCredential
            except ImportError:  # pragma: no cover
                raise ProviderError(
                    "keyless access needs the extra: pip install 'nuramesh[azure]' (and `az login`)"
                ) from None
            # no service-principal secret from the environment: keyless means your login or a
            # managed identity, nothing else
            # ND232: the library's own transport option. `use_env_settings=False` makes every
            # credential of the chain build its `RequestsTransport` with `trust_env` off (proxy
            # variables are never read), so no proxy sees an Entra access or refresh token or a
            # managed-identity secret. The library sends with `allow_redirects=False` and its
            # identity pipeline has no redirect policy, so a redirect is never followed either.
            # `az`, `azd` and PowerShell run out of process and inherit the proxy variables, so the
            # tool would refresh its token through the proxy. While any proxy variable is set they
            # are left out of the chain (fail closed, ND232). The VS Code member builds its own
            # broker client without the option, so it is always left out.
            proxied = built_proxied = proxy_variable_set()
            credential = DefaultAzureCredential(
                exclude_environment_credential=True,
                exclude_visual_studio_code_credential=True,
                exclude_cli_credential=proxied,
                exclude_developer_cli_credential=proxied,
                exclude_powershell_credential=proxied,
                use_env_settings=False,
            )
        try:
            return str(credential.get_token(SCOPE).token)
        except ProviderError:
            raise
        except Exception:
            if proxy_variable_set():
                # a fixed text: never the variable's value (it may hold credentials)
                raise ProviderError(
                    "keyless access failed: a proxy variable ("
                    + ", ".join(proxy_variables_set())
                    + ") is set, so the Azure CLI, azd and PowerShell logins are not used (a token "
                    "never goes through a proxy); unset it or use a managed identity"
                ) from None
            raise

    return token


def _opener() -> urllib.request.OpenerDirector:
    """An opener that never follows a redirect (it would carry the bearer token to another host) and
    never uses a proxy.
    Built when used, not when this module is imported: the handler class would otherwise be
    created at import time, which the Temporal workflow sandbox refuses."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            return None

    # an empty ProxyHandler: proxy environment variables are never read, so a proxy chosen by the
    # environment never sees the bearer token (ND225, T167)
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect, *net_guard.tracked_handlers()
    )


def _post(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    opener = _opener()
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        error.close()
        return error.code, b""  # the body can quote the prompt: never read or keep it
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise ProviderError("the service could not be reached") from None
    except (
        ValueError,
        UnicodeError,
    ):  # a malformed header value: never echo it (it holds the token)
        raise ProviderError("the request could not be sent") from None


class AzureOpenAIProvider:
    """A `ModelProvider` for one Azure OpenAI resource (the deployment name is the model name)."""

    cache_support: CacheSupport = "automatic"
    """OpenAI-style automatic prefix cache: no marker is ever sent (R1.34 5.3, 5.7)."""

    def __init__(
        self,
        endpoint: str,
        credential: CredentialGetter,
        *,
        data_class: str,
        provider_id: str = "azure-openai",
        token_field: str = "max_completion_tokens",
        processing: str,
        customer_regions: tuple[str, ...] = (),
        post: Post = _post,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not ENDPOINT.fullmatch(endpoint):
            raise ProviderError(
                f"{provider_id}: the endpoint must be https://<name>.openai.azure.com "
                "(the Entra token is only ever sent there)"
            )
        require_data_residency(processing, data_class, customer_regions=customer_regions)
        self._url = endpoint.rstrip("/") + "/openai/v1/chat/completions"
        self._credential = credential
        self._post = post
        self._timeout = timeout
        self._provider_id = provider_id
        self._token_field = token_field
        self.processing = processing
        self.data_class = data_class

    def complete(self, request: ModelRequest) -> ModelResponse:
        if request.provider != self._provider_id:
            raise ProviderError(f"{self._provider_id}: this provider only answers its own models")
        body = json.dumps(
            {
                "model": request.model,  # the deployment name
                "messages": [m.model_dump(mode="json") for m in request.messages],
                self._token_field: request.max_output_tokens,
            }
        ).encode("utf-8")
        try:
            got = self._credential()
        except ProviderError:
            raise
        except Exception:
            raise ProviderError(
                f"{self._provider_id}: no Entra token "
                "(run `az login`, and check your role assignment)"
            ) from None
        authorization = _authorization(got)
        if authorization is None:
            raise ProviderError(f"{self._provider_id}: the Entra token is not usable")
        headers = {"Authorization": authorization, "Content-Type": "application/json"}
        if self._post is not _post:
            call_signal.mark_sent()  # injected transport: sent from here (R20.8)
        status, payload = self._post(self._url, headers, body, self._timeout)
        if status in (401, 403):
            raise ProviderError(
                f"{self._provider_id}: not authorized ({status}): check `az login` and the role "
                f"assignment ({ROLE_HINT.get(self._provider_id, 'a role that allows inference')})"
            )
        if status == 429:
            raise ProviderError(f"{self._provider_id}: rate limited (429)")
        if status != 200:
            raise ProviderError(f"{self._provider_id}: the service answered {status}")
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ProviderError(f"{self._provider_id}: the answer was larger than the limit")
        return _parse(payload, self._provider_id)


def _usage_of(data: object) -> TokenUsage | None:
    """The tokens the service reported, or None when missing or invalid (never a guess): the
    engine then charges the whole reservation as an unknown cost (#1459)."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if not _valid_count(prompt) or not _valid_count(completion):
        return None
    assert isinstance(prompt, int) and isinstance(completion, int)
    details = usage.get("prompt_tokens_details")
    cached: object = details.get("cached_tokens") if isinstance(details, dict) else None
    if cached is None:
        cached = 0
    if not _valid_count(cached):
        return None
    assert isinstance(cached, int)
    cached = min(cached, prompt)
    return TokenUsage(
        input_tokens=prompt - cached, output_tokens=completion, cached_input_tokens=cached
    )


def _valid_count(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_TOKENS


def _parse(payload: bytes, provider_id: str) -> ModelResponse:
    try:
        data = json.loads(payload)
    except (ValueError, OverflowError):
        raise ProviderError(f"{provider_id}: the answer could not be read") from None
    token_usage = _usage_of(data)
    try:
        choice = data["choices"][0]
        reason = choice["finish_reason"]
        content = choice["message"]["content"]
    except (KeyError, IndexError, TypeError, AttributeError):
        if token_usage is None:
            raise ProviderError(f"{provider_id}: the answer could not be read") from None
        # valid usage but unreadable choices: the call was billed, so it is charged (never
        # released) and surfaces as a filtered answer with no text (#1459)
        return ModelResponse(text="", usage=token_usage, finish_reason="filtered")
    # From here the service answered: it must be charged, so we return a response instead of
    # raising (a ProviderError means "not charged" to the engine). `token_usage` None (missing or
    # invalid usage) is charged as the whole reservation, cost unknown (#1459).
    if reason == "stop" and isinstance(content, str):
        return ModelResponse(text=content, usage=token_usage, finish_reason="stop")
    if reason == "length" and isinstance(content, str):
        return ModelResponse(text=content, usage=token_usage, finish_reason="length")
    # content_filter, tool_calls, an empty answer cut off while reasoning...: no usable answer
    return ModelResponse(text="", usage=token_usage, finish_reason="filtered")


def probe_url(endpoint: str) -> str:
    """The URL of the probe for a connection's endpoint."""
    return endpoint.rstrip("/") + PROBE_PATH


def http_get(url: str, headers: dict[str, str], timeout: float) -> int:
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    opener = _opener()
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status)  # the body is never read
    except urllib.error.HTTPError as error:
        error.close()
        return int(error.code)  # includes 3xx: a redirect is reported, never followed
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
        raise OSError("unreachable") from None
    except (ValueError, UnicodeError):
        raise OSError("unreachable") from None


@dataclass(frozen=True)
class ProbeResult:
    """What one probe found. `code` is one of the test-connection codes (spec section 11);
    `status_class` is `2xx`..`5xx` when the endpoint answered."""

    code: str
    status_class: str | None
    latency_ms: int
    called: bool


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def probe(
    endpoint: str,
    credential: CredentialGetter,
    *,
    transport: ProbeTransport = http_get,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = PROBE_TIMEOUT,
) -> ProbeResult:
    """One authenticated, body-less `GET` of the model listing under `endpoint`. Never raises: a
    missing credential, an unreachable endpoint and every status become a result. No message or
    result holds the token, a header or a body."""
    try:
        got = credential()
    except Exception:
        return ProbeResult("credential_unavailable", None, 0, False)
    authorization = _authorization(got)
    if authorization is None:
        return ProbeResult("credential_unavailable", None, 0, False)
    headers = {"Authorization": authorization, "User-Agent": "nuramesh-connection-test"}
    started = clock()
    try:
        status = transport(probe_url(endpoint), headers, timeout)
    except (OSError, TimeoutError):
        return ProbeResult("unreachable", None, _ms(clock() - started), True)
    latency = _ms(clock() - started)
    klass = f"{status // 100}xx"
    shown = klass if klass in ("2xx", "3xx", "4xx", "5xx") else None
    if 200 <= status < 300:
        code = "ok"
    elif status in (401, 403):
        code = "auth_failed"
    elif status == 404:
        code = "not_found"
    elif 300 <= status < 400:
        code = "redirected"
    elif status >= 500:
        code = "server_error"
    else:
        code = "unexpected_status"
    return ProbeResult(code, shown, latency, True)


PROBE_TEXTS = {
    "ok": "the endpoint answered and accepted the Entra login",
    "credential_unavailable": (
        "no Entra credential is available here (az login, or a managed identity)"
    ),
    "auth_failed": (
        "the endpoint refused the Entra login (HTTP 401 or 403): check the role assignment on "
        "the account"
    ),
    "not_found": "the endpoint answered 404 for the probe: check the endpoint address",
    "redirected": "the endpoint tried to redirect the probe; redirects are never followed",
    "server_error": "the endpoint answered with a server error (HTTP 5xx)",
    "unreachable": (
        "the endpoint could not be reached (connection failed or timed out after 10 seconds)"
    ),
}


def probe_text(code: str, status_class: str | None = None) -> str:
    """The fixed text for a probe result code (spec section 11)."""
    if code == "unexpected_status":
        return f"the endpoint answered with an unexpected status (HTTP {status_class or 'other'})"
    return PROBE_TEXTS[code]
