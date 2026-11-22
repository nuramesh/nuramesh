"""The OpenAI-compatible endpoint provider (spec/openai-compatible, ND259, ND260, ND262).

One named connection (`openai-compatible/<endpoint id>`) is one `base_url` and an auth of `none`
or `key`. The adapter makes a non-streaming `POST {base_url}/chat/completions` and holds no
vendor list, no model list and no price. It is the same adapter for OpenAI direct, Ollama, vLLM,
LM Studio, the llama.cpp server, Hugging Face TGI and Endpoints, Groq, Together and Gemini's
OpenAI API: only the `base_url` differs.

Rules this module keeps (section numbers are the spec's):

- 3: one URL function (`connection_problem`). `https` to any host; `http` only to the literal
  `127.0.0.1` or `[::1]`, never with a key; no user, query or fragment; no `localhost` name.
- 4: a key comes only from the operator's binding file (`NURAMESH_OPENAI_COMPATIBLE_KEYS`), which
  binds each key name to a key file and to the `host:port` it may be sent to. Every binding check
  runs before any key file is opened, and no text holds a path, a key or the URL.
- 5: no proxy (ND232) and no redirect; certificate checks are the library default and nothing in
  this module can turn them off.
- 7: a served answer with missing or invalid usage returns `usage=None`: the cost is unknown and
  the reservation is charged (never zero).
- 8: only a failure that certainly processed nothing is retried (429, 503 with a valid
  `Retry-After`, connection refused before a byte was sent); every attempt is recorded.
- 10: a remote endpoint takes only `global` processing (`core.endpoints.residency_problem`).
"""

import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from nuramesh.core.endpoints import is_loopback, normalise, residency_problem
from nuramesh.core.models import (
    MAX_MESSAGE_CHARS,
    CacheSupport,
    ModelRequest,
    ModelResponse,
    ProviderError,
)
from nuramesh.core.org import OPENAI_COMPATIBLE, EndpointId
from nuramesh.core.prices import MAX_TOKENS, TokenUsage
from nuramesh.core.residency import require_data_residency
from nuramesh.core.secrets_text import CredentialGetter, Redacted, reveal_secret
from nuramesh.engine import call_signal, key_file, net_guard
from nuramesh.engine.key_file import KeyTexts
from nuramesh.hosted import HOSTED_ENV_PROBLEM, HostedConfigError, hosted_from_env

BINDINGS_ENV = "NURAMESH_OPENAI_COMPATIBLE_KEYS"
KEY_PATH_ENVS = (
    "NURAMESH_ANTHROPIC_KEY_FILE",
    "NURAMESH_ANCHOR_SSH_KEY",
    "NURAMESH_OTEL_PSEUDONYM_KEY_FILE",
    "NURAMESH_REFRESH_SUCCESSOR_KEY",
)
KEY_DIR_ENV = "NURAMESH_KEY_DIR"
USER_AGENT = "nuramesh-openai-compatible/1"
MAX_RESPONSE_BYTES = 8_000_000
MAX_BINDING_BYTES = 16_384
MAX_NONSTREAMING_OUTPUT_TOKENS = 16000
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER = 30
DEFAULT_TIMEOUT = 300.0
PROBE_TIMEOUT = 10.0
MAX_PATH_CHARS = 200

_P = "openai-compatible: "
_KEY_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
_HOST_PORT = re.compile(r"(\[[0-9a-f:]+\]|[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?):[0-9]{1,5}")
_WHOLE_NUMBER = re.compile(r"[0-9]{1,9}")
_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")
_ESCAPES = re.compile(r"%2[ef]", re.IGNORECASE)

# -- fixed texts (spec section 18); none holds the input, a URL, a key, a header or server text --
U_BAD = f"{_P}the address is not valid"
U_PARTS = f"{_P}the address must not contain a user, a query or a fragment"
U_HTTP = f"{_P}http is only allowed to 127.0.0.1 or [::1]"
U_KEYHTTP = f"{_P}a key is never sent over http"
U_AUTH = f"{_P}auth must be none or key"
U_KEYNAME_NEEDED = f"{_P}auth key needs a key name"
U_KEYNAME_FORBIDDEN = f"{_P}auth none takes no key name"
U_KEYNAME_STORED = f"{_P}auth stored takes no key name"
U_KEYNAME_BAD = f"{_P}the key name is not valid"
U_ID = f"{_P}the endpoint id is not valid"
U_ENDPOINT_NEEDED = (
    f"{_P}this provider needs an endpoint id (openai-compatible/<endpoint id>/<model>)"
)
U_FIELD = f"{_P}a key file path cannot be given; the operator binds key files"
H_TEXT = (
    f"{_P}hosted needs a secret store and an egress policy (a later item); "
    "this provider is for self-hosted use only"
)
H_FLAG_TEXT = f"{_P}{HOSTED_ENV_PROBLEM}"
KB_NONE = f"{_P}no key bindings (the operator sets {BINDINGS_ENV} to the path of the binding file)"
KB_FILE = f"{_P}the key binding file is not usable"
KB_NAME = f"{_P}no key with that name is bound by the operator"
KB_HOST = f"{_P}that key is not bound to this host"
KB_OTHER = f"{_P}that key file belongs to something else and cannot be bound here"
K_NOTUSABLE = f"{_P}the API key is not usable"
START_LINE = "openai-compatible: the key binding file is not usable"
R_PROVIDER = f"{_P}this provider only answers its own models"
R_REDIRECT = f"{_P}the service tried to redirect; redirects are refused"
R_UNREACHABLE = f"{_P}the service could not be reached"
R_PRIVATE = f"{_P}{net_guard.REFUSED_TEXT}"
R_UNREADABLE = f"{_P}the answer could not be read"
R_LARGE = f"{_P}the answer was larger than the limit"
R_TOKENS = f"{_P}max_output_tokens is above 16000"
R_SHAPE_USER = f"{_P}the request needs a user message"
R_SHAPE_LAST = f"{_P}the last message must be from the user"
R_SHAPE_EMPTY = f"{_P}a message has no content"
R_ENCODE = f"{_P}the request could not be sent"
R_RESIDENCY = f"{_P}a remote endpoint can only declare global processing"
CONNECTION_CHANGED = f"{_P}the connection changed since this model was approved; approve it again"
PROBE_404 = f"{_P}reachable; the model list is not available (the call itself may still work)"
PROBE_CODES = frozenset(
    {
        "ok",
        "unauthorized",
        "not_found",
        "redirect",
        "server_error",
        "unexpected_status",
        "unreachable",
        "refused",
    }
)
"""Every `code` `probe` can return (the connection history checks a tested row against them)."""
_STATUS_TEXTS = {
    400: "the service rejected the request (400)",
    402: "billing problem on the account (402)",
    404: "model or path not found (404)",
    413: "the request was too large (413)",
    422: "the service could not process the request (422)",
}


def _auth_text(code: int, key: bool) -> str:
    tail = (
        "check the operator's key binding for this connection"
        if key
        else "the endpoint wants a key; connect it with a key"
    )
    return f"{_P}not authorized ({code}): {tail}"


_FIXED = "\n".join(
    [
        U_BAD, U_PARTS, U_HTTP, U_KEYHTTP, U_AUTH, U_KEYNAME_NEEDED, U_KEYNAME_FORBIDDEN,
        U_KEYNAME_STORED,
        U_KEYNAME_BAD, U_ID, U_ENDPOINT_NEEDED, U_FIELD, H_TEXT, KB_NONE, KB_FILE, KB_NAME,
        KB_HOST, KB_OTHER, K_NOTUSABLE, R_PROVIDER, R_REDIRECT, R_UNREACHABLE, R_UNREADABLE,
        R_LARGE, R_TOKENS, R_SHAPE_USER, R_SHAPE_LAST, R_SHAPE_EMPTY, R_ENCODE, R_RESIDENCY,
        CONNECTION_CHANGED, PROBE_404, *_STATUS_TEXTS.values(), "[redacted]",
    ]
)  # fmt: skip
KEY_TEXTS = KeyTexts(_P, K_NOTUSABLE, K_NOTUSABLE, _FIXED)  # an empty file is "not usable" (K3)
_BINDING_TEXTS = KeyTexts(_P, KB_NONE, KB_FILE, "")

Post = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes, Mapping[str, str]]]
Get = Callable[[str, dict[str, str], float], tuple[int, Mapping[str, str]]]


# ---------------------------------------------------------------- hosted mode (3.7)


def hosted_problem() -> str | None:
    """The fixed text when this provider is refused because the server is hosted (or the flag is
    invalid), else None. Never treats a bad flag as "not hosted"."""
    hosted, problem = False, None
    try:
        hosted = hosted_from_env()
    except HostedConfigError:
        problem = H_FLAG_TEXT
    if problem is not None:
        return problem
    return H_TEXT if hosted else None


# ---------------------------------------------------------------- URL rules (3)


def key_name_problem(auth: str, key: object) -> str | None:
    """The fixed text for an auth/key-name pair (2.3, U12), or None."""
    if auth not in ("none", "key", "stored"):
        return U_AUTH
    if auth == "none":
        return U_KEYNAME_FORBIDDEN if key is not None else None
    if auth == "stored":  # the key is a stored model key named by `key_ref`, not by a name
        return U_KEYNAME_STORED if key is not None else None
    if key is None:
        return U_KEYNAME_NEEDED
    if not isinstance(key, str) or not _KEY_NAME.fullmatch(key):
        return U_KEYNAME_BAD
    return None


def endpoint_id_problem(provider: str, endpoint_id: object) -> str | None:
    """The check order of 2.1 for the provider and endpoint id (the provider is already known to
    be supported). Returns the fixed code or text, or None."""
    if provider != OPENAI_COMPATIBLE:
        return "endpoint_id_not_allowed" if endpoint_id is not None else None
    if endpoint_id is None:
        return U_ENDPOINT_NEEDED
    if not isinstance(endpoint_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,39}", endpoint_id
    ):
        return U_ID
    return None


def _bad_chars(raw: str) -> bool:
    return (
        raw != raw.strip()
        or "\\" in raw
        or not raw.isascii()
        or any(ord(c) < 0x21 or ord(c) == 0x7F for c in raw)
    )


def connection_problem(
    provider: str,
    endpoint: str,
    auth: str,
    key: str | None = None,
    endpoint_id: str | None = None,
) -> str | None:
    """Why a connection is refused (the URL rules of 3 and the auth rules of 2.3), or None.

    The raw string is judged (nothing is stripped first). The input is never quoted."""
    code = endpoint_id_problem(provider, endpoint_id)
    if code is not None:
        return code
    if not isinstance(endpoint, str) or not endpoint or _bad_chars(endpoint):
        return U_BAD
    try:
        parts = urlsplit(endpoint)
        port = parts.port
        host = parts.hostname
    except ValueError:
        return U_BAD
    if parts.scheme not in ("http", "https") or not endpoint.lower().startswith(
        f"{parts.scheme}://"
    ):
        return U_BAD
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return U_PARTS
    if parts.query or parts.fragment or "?" in endpoint or "#" in endpoint:
        return U_PARTS
    if not host or (port is not None and not 1 <= port <= 65535):
        return U_BAD
    path = parts.path
    if len(path) > MAX_PATH_CHARS or ".." in path or "//" in path or _ESCAPES.search(path):
        return U_BAD
    if parts.scheme == "http" and not is_loopback(endpoint):
        return U_HTTP
    if parts.scheme == "http" and auth in ("key", "stored"):
        return U_KEYHTTP
    return key_name_problem(auth, key)


def request_url(endpoint: str) -> str:
    """`base_url + "/chat/completions"` with the trailing slash of the base removed (U10)."""
    return f"{normalise(endpoint)}/chat/completions"


def host_port(endpoint: str) -> str | None:
    """The normalised `host:port` (default port filled in) of an endpoint, or None."""
    try:
        parts = urlsplit(endpoint)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


# ---------------------------------------------------------------- operator bindings (4)


@dataclass(frozen=True)
class Binding:
    """One key name: the key file and the `host:port` strings it may be sent to."""

    file: str
    hosts: tuple[str, ...]


def _real(path: str) -> str:
    return os.path.realpath(path)


def _under(path: str, directory: str) -> bool:
    base = _real(directory)
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _bindings_path() -> str | None:
    value = os.environ.get(BINDINGS_ENV)
    return value if value else None


def load_bindings(agent: key_file.IsAgentUser | None = None) -> dict[str, Binding]:
    """The operator binding file, read now (never cached). `ProviderError` with KB-none or
    KB-file; the paths and content are never in a text (4.1)."""
    path = _bindings_path()
    if path is None:
        raise ProviderError(KB_NONE)
    data = b""
    failed = False
    try:
        data = key_file.read_checked_file(
            path,
            _BINDING_TEXTS,
            agent=agent or key_file.is_agent_user,
            limit=MAX_BINDING_BYTES,
        )
    except ProviderError:
        failed = True
    if failed:
        raise ProviderError(KB_FILE)
    parsed = _parse_bindings(data)
    if parsed is None:
        raise ProviderError(KB_FILE)
    for binding in parsed.values():  # a key file that is something else's key is KB-other
        if _other_key(binding.file, path):
            raise ProviderError(KB_OTHER)
    return parsed


def _parse_bindings(data: bytes) -> dict[str, Binding] | None:
    """The bindings of a file, or None for a bad shape, an unknown field or a duplicate name."""
    duplicate = False

    def hook(items: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate
        names = [k for k, _ in items]
        duplicate = duplicate or len(names) != len(set(names))
        return dict(items)

    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=hook)
    except (ValueError, UnicodeError):
        return None
    if duplicate or not isinstance(raw, dict) or set(raw) != {"keys"}:
        return None
    if not isinstance(raw["keys"], dict):
        return None
    out: dict[str, Binding] = {}
    for name, item in raw["keys"].items():
        if not isinstance(name, str) or not _KEY_NAME.fullmatch(name):
            return None
        if not isinstance(item, dict) or set(item) != {"file", "hosts"}:
            return None
        file, hosts = item["file"], item["hosts"]
        if not isinstance(file, str) or not isinstance(hosts, list) or not 1 <= len(hosts) <= 8:
            return None
        for h in hosts:
            if not isinstance(h, str) or not _HOST_PORT.fullmatch(h):
                return None
        out[name] = Binding(file, tuple(hosts))
    return out


def _other_key(file: str, binding_path: str) -> bool:
    """True when `file` is the binding file itself or another setting's key (4.2 step 5)."""
    real = _real(file)
    if real == _real(binding_path):
        return True
    for env in KEY_PATH_ENVS:
        other = os.environ.get(env)
        if other and real == _real(other):
            return True
    directory = os.environ.get(KEY_DIR_ENV)
    return bool(directory and _under(real, directory))


def startup_problem(agent: key_file.IsAgentUser | None = None) -> str | None:
    """The one start line when the binding file is set but unusable (4.1, K17), else None.

    Skipped in hosted mode; an unset variable is not an error."""
    if hosted_problem() is not None or _bindings_path() is None:
        return None
    try:
        load_bindings(agent)
    except ProviderError:
        return START_LINE
    return None


def key_for(endpoint: str, key_name: str, *, agent: key_file.IsAgentUser | None = None) -> str:
    """The key for `key_name` at `endpoint`, after every check of 4.2 (hosted, binding file, name,
    host, other-key) and only then the key file. `ProviderError` with a fixed text."""
    problem = hosted_problem()
    if problem is not None:
        raise ProviderError(problem)
    bindings = load_bindings(agent)
    binding = bindings.get(key_name)
    if binding is None:
        raise ProviderError(KB_NAME)
    if host_port(endpoint) not in binding.hosts:
        raise ProviderError(KB_HOST)
    return key_file.read_key_file(binding.file, KEY_TEXTS, agent=agent or key_file.is_agent_user)


# ---------------------------------------------------------------- transport (5)


def opener() -> urllib.request.OpenerDirector:
    """No proxy (ND232: proxy variables are never read, for `none` and `key` alike) and no
    redirect. Certificate checks are the library default; nothing here changes them. The handler
    class is built when used, not at import: the Temporal workflow sandbox refuses a module that
    subclasses an `urllib` handler while it is imported."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            return None

    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect, *net_guard.guarded_handlers()
    )


def _pick(headers: Any) -> dict[str, str]:
    value = headers.get("retry-after") if headers is not None else None
    return {"retry-after": value} if isinstance(value, str) else {}


def default_post(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes, Mapping[str, str]]:
    """One POST through `opener`. The body of an error answer is never read."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    caught: urllib.error.HTTPError | None = None
    try:
        with opener().open(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(MAX_RESPONSE_BYTES + 1), _pick(response.headers)
    except urllib.error.HTTPError as error:
        caught = error
    assert caught is not None
    kept = _pick(caught.headers)
    caught.close()
    return caught.code, b"", kept


def default_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, Mapping[str, str]]:
    """One body-less GET through `opener` for the probe; the body is discarded."""
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    caught: urllib.error.HTTPError | None = None
    try:
        with opener().open(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), {}
    except urllib.error.HTTPError as error:
        caught = error
    assert caught is not None
    caught.close()
    return int(caught.code), {}


def _private_refusal(error: BaseException) -> bool:
    """The request was refused by the private-network guard (nothing was sent)."""
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    return isinstance(cause, net_guard.PrivateNetworkError)


def _refused_before_sending(error: BaseException) -> bool:
    """A connection refused on connect: nothing was sent, so a retry cannot bill twice."""
    cause = error.reason if isinstance(error, urllib.error.URLError) else error
    return isinstance(cause, ConnectionRefusedError)


# ---------------------------------------------------------------- request and response (6, 7)


def _shape_problem(request: ModelRequest) -> str | None:
    non_system = [m for m in request.messages if m.role != "system"]
    if not non_system:
        return R_SHAPE_USER
    if non_system[-1].role != "user":
        return R_SHAPE_LAST
    if any(not m.content for m in request.messages):
        return R_SHAPE_EMPTY
    if request.max_output_tokens > MAX_NONSTREAMING_OUTPUT_TOKENS:
        return R_TOKENS
    return None


def _valid_count(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_TOKENS


def usage_of(data: Mapping[str, Any]) -> TokenUsage | None:
    """The usage of a served answer (7.3), or None when absent or invalid (7.4). Cached-token
    details are ignored: the whole prompt is charged at the input rate (the safe side)."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if type(prompt) is not int or type(completion) is not int:
        return None
    if not _valid_count(prompt) or not _valid_count(completion):
        return None
    return TokenUsage(input_tokens=prompt, output_tokens=completion)


def parse_answer(payload: bytes) -> ModelResponse:
    """The mapping of 7.1 and 7.2. Raises `ProviderError` (not charged) when the answer is not
    readable (7.5); an invalid usage still returns the answer with `usage=None` (7.4)."""
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ProviderError(R_LARGE)
    data: Any = None
    try:
        data = json.loads(payload)
    except (ValueError, UnicodeError):
        data = None
    choices = data.get("choices") if isinstance(data, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else 0
    if not isinstance(data, dict) or not isinstance(message, dict):
        raise ProviderError(R_UNREADABLE)
    if content is not None and not isinstance(content, str):
        raise ProviderError(R_UNREADABLE)  # a list of content parts is not accepted in v1
    text = content or ""
    if len(text) > MAX_MESSAGE_CHARS or _SURROGATE.search(text):
        raise ProviderError(R_UNREADABLE)
    reason = first.get("finish_reason") if isinstance(first, dict) else None
    if reason == "length":
        finish = "length"
    elif reason == "content_filter" or not text:
        finish = "filtered"
    else:
        finish = "stop"
    return ModelResponse(text=text, usage=usage_of(data), finish_reason=finish)  # type: ignore[arg-type]


def backoff(retry_after: str | None, attempt: int, *, status: int) -> float | None:
    """Seconds to wait after attempt `attempt` (8.1), or None when the answer says not to retry."""
    if retry_after is not None and _WHOLE_NUMBER.fullmatch(retry_after):
        seconds = int(retry_after)
        if seconds > MAX_RETRY_AFTER:
            return None
        if seconds >= 1:
            return float(seconds)
    if status == 503:
        return None  # a 503 is retried only with a valid Retry-After
    return float(2 ** (attempt - 1))


# ---------------------------------------------------------------- the provider


def _bearer(credential: object) -> str | None:
    """The `Authorization` value for a credential (the one place this adapter reveals it), or None
    when it is not a usable `Redacted` value."""
    if not isinstance(credential, Redacted):
        return None
    return f"Bearer {reveal_secret(credential)}"


class OpenAICompatibleProvider:
    """A `ModelProvider` for one connection of an OpenAI-compatible server (non-streaming)."""

    cache_support: CacheSupport = "none"
    """Cached tokens are ignored (#421), so a marker would buy nothing (R1.34 5.3)."""

    def __init__(
        self,
        *,
        endpoint_id: EndpointId,
        endpoint: str,
        auth: str,
        key: str | None = None,
        data_class: str,
        processing: str,
        post: Post = default_post,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
        credential: CredentialGetter | None = None,
    ) -> None:
        problem = connection_problem(OPENAI_COMPATIBLE, endpoint, auth, key, endpoint_id)
        if problem is not None:
            raise ProviderError(problem)
        residency = residency_problem(endpoint, processing)
        if residency is not None:
            raise ProviderError(R_RESIDENCY)
        require_data_residency(processing, data_class)
        self.endpoint_id = endpoint_id
        self.endpoint = normalise(endpoint)
        self.auth = auth
        self._key_name = key
        self._post = post
        self._sleep = sleep
        self._timeout = timeout
        self._credential = credential
        self.processing = processing
        self.data_class = data_class
        self._local = threading.local()

    @property
    def attempts(self) -> list[dict[str, Any]]:
        """The attempts of the last call made on this thread: `{"n", "outcome"}` (a status,
        `refused` or `unreachable`); never a header, body or URL (8.2). The adapter is cached and
        shared by the run engine's worker threads, so the list belongs to the calling thread and
        one call can never see the attempts of another."""
        found: list[dict[str, Any]] | None = getattr(self._local, "attempts", None)
        if found is None:
            found = self._local.attempts = []
        return found

    def __repr__(self) -> str:
        return f"OpenAICompatibleProvider(endpoint_id={self.endpoint_id!r})"

    def complete(self, request: ModelRequest) -> ModelResponse:
        failed = self._run(request)
        if isinstance(failed, str):
            raise ProviderError(failed)
        return failed

    def _headers(self) -> dict[str, str] | str:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self.auth not in ("key", "stored"):
            return headers
        assert self.auth == "stored" or self._key_name is not None
        got: object = None
        text: str | None = None
        try:
            got = self._credential() if self._credential is not None else None
        except ProviderError as error:
            text = str(error.args[0]) if error.args else K_NOTUSABLE
        except Exception:
            text = K_NOTUSABLE
        if text is not None:
            return text
        bearer = _bearer(got)
        del got
        if bearer is None:
            return K_NOTUSABLE
        headers["Authorization"] = bearer
        return headers

    def _run(self, request: ModelRequest) -> ModelResponse | str:
        self._local.attempts = []
        problem = hosted_problem()
        if problem is not None:
            return problem
        if request.provider != OPENAI_COMPATIBLE or request.endpoint_id != self.endpoint_id:
            return R_PROVIDER
        shape = _shape_problem(request)
        if shape is not None:
            return shape
        body = {
            "model": request.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        encoded = b""
        bad = False
        try:
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        except (ValueError, UnicodeError):
            bad = True
        if bad:
            return R_ENCODE
        headers = self._headers()
        if isinstance(headers, str):
            return headers
        result = self._attempt_loop(headers, encoded)
        key = headers.get("Authorization", "")[len("Bearer ") :]
        headers.clear()
        if isinstance(result, str) and key:
            return result.replace(key, "[redacted]")
        return result

    def _attempt_loop(self, headers: dict[str, str], encoded: bytes) -> ModelResponse | str:
        url = request_url(self.endpoint)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            reply: Any = None
            outcome: str | None = None
            refused = False
            try:
                if self._post is not default_post:
                    call_signal.mark_sent()  # injected transport: sent from here (R20.8)
                reply = self._post(url, headers, encoded, self._timeout)
            except (ValueError, UnicodeError):
                outcome = "unreachable"
            except (ssl.SSLError, socket.gaierror, TimeoutError):
                outcome = "unreachable"
            except Exception as error:
                if _private_refusal(error):
                    self.attempts.append({"n": attempt, "outcome": "private_network"})
                    return R_PRIVATE  # a setting, not a fault: never retried
                outcome = "refused" if _refused_before_sending(error) else "unreachable"
                refused = outcome == "refused"
            if outcome is not None:
                self.attempts.append({"n": attempt, "outcome": outcome})
                if refused and attempt < MAX_ATTEMPTS and self._slept(float(2 ** (attempt - 1))):
                    continue  # nothing was sent: wait 1 s, then 2 s (8.1) and try again
                return R_UNREACHABLE
            code, payload, reply_headers = reply
            self.attempts.append({"n": attempt, "outcome": code})
            if code == 200:
                return self._parse(payload)
            if 300 <= code < 400:
                return R_REDIRECT
            if code in (429, 503) and attempt < MAX_ATTEMPTS:
                wait = backoff(reply_headers.get("retry-after"), attempt, status=code)
                if wait is not None and self._slept(wait):
                    continue
            return self._status_text(code)
        return R_UNREACHABLE  # pragma: no cover

    def _slept(self, wait: float) -> bool:
        try:
            self._sleep(wait)
        except Exception:
            return False
        return True

    def _status_text(self, code: int) -> str:
        if code in (401, 403):
            return _auth_text(code, self.auth in ("key", "stored"))
        if code in _STATUS_TEXTS:
            return f"{_P}{_STATUS_TEXTS[code]}"
        return f"{_P}the service answered {code}"

    @staticmethod
    def _parse(payload: bytes) -> ModelResponse | str:
        parsed: ModelResponse | None = None
        text: str | None = None
        try:
            parsed = parse_answer(payload)
        except ProviderError as error:
            text = str(error.args[0])
        if text is not None:
            return text
        assert parsed is not None
        return parsed


# ---------------------------------------------------------------- probe (9)


@dataclass(frozen=True)
class ProbeFound:
    """What one probe found; never a header, a body or a key."""

    code: str
    detail: str | None
    latency_ms: int
    called: bool


def probe(
    endpoint: str,
    auth: str,
    key_name: str | None,
    *,
    get: Get = default_get,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = PROBE_TIMEOUT,
    credential: CredentialGetter | None = None,
) -> ProbeFound:
    """One body-less `GET {base_url}/models` (no prompt, no spend). Never raises. A binding
    refusal comes back as `refused` with the fixed text and no request is made."""
    problem = hosted_problem()
    if problem is not None:
        return ProbeFound("refused", problem, 0, False)
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if auth == "key":
        text: str | None = None
        got: object = None
        try:
            got = credential() if credential is not None else None
        except ProviderError as error:
            text = str(error.args[0]) if error.args else K_NOTUSABLE
        except Exception:
            text = K_NOTUSABLE
        if text is not None:
            return ProbeFound("refused", text, 0, False)
        bearer = _bearer(got)
        del got
        if bearer is None:
            return ProbeFound("refused", K_NOTUSABLE, 0, False)
        headers["Authorization"] = bearer
    started = clock()
    status = 0
    unreachable = False
    try:
        status, _ = get(f"{normalise(endpoint)}/models", headers, timeout)
    except Exception as error:
        if _private_refusal(error):
            headers.clear()
            return ProbeFound("refused", R_PRIVATE, 0, False)
        unreachable = True
    headers.clear()
    latency = max(0, round((clock() - started) * 1000))
    if unreachable:
        return ProbeFound("unreachable", None, latency, True)
    if 200 <= status < 300:
        code, detail = "ok", None
    elif status in (401, 403):
        code, detail = "unauthorized", None
    elif status == 404:
        code, detail = "not_found", PROBE_404
    elif 300 <= status < 400:
        code, detail = "redirect", None
    elif status >= 500:
        code, detail = "server_error", None
    else:
        code, detail = "unexpected_status", None
    return ProbeFound(code, detail, latency, True)
