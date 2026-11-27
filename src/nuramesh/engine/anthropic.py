"""The Anthropic direct model provider: non-streaming Messages API, key from a key file (R1.14b).

`AnthropicProvider.complete` sends one request to the Messages API (`API_URL`) and maps
the answer to a `ModelResponse`. The design notes (rule numbers below refer to it).

- **Key (4.1, 4.2).** Only from a `0400` or `0600` file outside every repository, owned by
  the server user and not by the agent account (ND156), self-hosted only (hosted is refused
  until a secret store exists), named by `NURAMESH_ANTHROPIC_KEY_FILE` (a path, never a
  secret). Never from the environment.
- **No exception chaining (7.1a).** Nothing here raises inside an `except` or `finally` block: a
  handler records a code, the error is raised after it, so neither `__cause__` nor `__context__`
  can hold the key. `_run` returns a failure message instead of raising, so the frames that held
  the key are gone before the `ProviderError` is raised in `complete`.
- **Charged or not (5).** Until a JSON object answer is read a failure is `ProviderError` (not
  charged); from then on the call is billed and a `ModelResponse` is always returned. Missing or
  invalid `usage` returns `usage=None`: the engine charges the whole reservation, cost unknown
  (#1459).
- **Global only (8.1).** The direct API has no Canada-only guarantee: `processing` must be
  `global`, which `core.residency` limits to synthetic data.
- The adapter holds no model list, no price and no endpoint setting; the transport is injectable.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from nuramesh.core.models import (
    MAX_MESSAGE_CHARS,
    CacheSupport,
    ModelRequest,
    ModelResponse,
    ProviderError,
)
from nuramesh.core.prices import MAX_TOKENS, TokenUsage
from nuramesh.core.residency import ResidencyError, require_data_residency
from nuramesh.core.secrets_text import CredentialGetter, Redacted, reveal_secret
from nuramesh.engine import call_signal, net_guard
from nuramesh.engine.key_file import (
    KeyTexts,
    is_agent_user,
)
from nuramesh.engine.key_file import read_key_file as _read_key_file
from nuramesh.hosted import HOSTED_ENV_PROBLEM, HostedConfigError, hosted_from_env

ENDPOINT = "https://api.anthropic.com"
"""The one endpoint of a connection to this provider (not writable, rule 11.2)."""
API_URL = f"{ENDPOINT}/v1/messages"
API_VERSION = "2026-10-21"
USER_AGENT = "nuramesh-anthropic/1"
KEY_FILE_ENV = "NURAMESH_ANTHROPIC_KEY_FILE"
KEY_FILE_PROVIDERS = frozenset({"anthropic"})
PROBE_URL = f"{ENDPOINT}/v1/models?limit=1"
PROBE_TIMEOUT = 10.0
"""The coding agent's account: a key file it could read is refused (ND156, ND214)."""
MAX_NONSTREAMING_OUTPUT_TOKENS = 16000
MAX_RESPONSE_BYTES = 8_000_000
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER = 30
DEFAULT_TIMEOUT = 300.0

_KEY_SHAPE = re.compile(r"[\x21-\x7e]{10,512}")
_KEY_SHAPE_BYTES = re.compile(rb"[\x21-\x7e]{10,512}")
_REQUEST_ID = re.compile(r"req_[A-Za-z0-9]{8,64}")
_WHOLE_NUMBER = re.compile(r"[0-9]{1,9}")
_RETRIED = frozenset({429, 500, 502, 503, 529})
_KEPT_HEADERS = ("retry-after", "request-id")

_P = "anthropic: "
NO_KEY = f"{_P}no API key (set {KEY_FILE_ENV} to the path of a key file)"
KEY_NOT_USABLE = f"{_P}the API key is not usable"
_FILE_REASONS = (
    "not an absolute path",
    "inside a repository",
    "not a regular file",
    "cannot be read",
    "not owned by the server user",
    "permissions must be 0400 or 0600",
    "readable by the agent user",
    "larger than 1024 bytes",
)
HOSTED_FLAG_TEXT = f"{_P}{HOSTED_ENV_PROBLEM}"  # the shared parser's text, said by this adapter
HOSTED_TEXT = (
    f"{_P}hosted needs a secret store (a later item); a key file is for self-hosted use only"
)


def _file_text(reason: str) -> str:
    return f"{_P}the API key file is not usable ({reason})"


_KEY_FILE_TEXTS = frozenset(
    {
        NO_KEY,
        KEY_NOT_USABLE,
        HOSTED_TEXT,
        HOSTED_FLAG_TEXT,
        *(_file_text(r) for r in _FILE_REASONS),
    }
)
_STATUS_TEXTS = {
    400: "the service rejected the request (400)",
    401: f"not authorized (401): check the API key file ({KEY_FILE_ENV})",
    402: "billing problem on the account (402)",
    403: "not permitted for this key (403)",
    404: "model not found or not available to this key (404)",
    413: "the request was too large (413)",
    429: "rate limited (429)",
    529: "the service is overloaded (529)",
}
_OTHER_TEXTS = (
    "this request cannot be sent to the Messages API (",
    "this provider only answers its own models",
    "the service tried to redirect; redirects are refused",
    "the service could not be reached",
    "the request could not be sent",
    "the answer could not be read",
    "the usage in the answer is not valid",
    "the answer was larger than the limit",
    "the service answered ",
    "[redacted]",
    " [request ",
)
# A key inside any of these would be altered by the redaction of rule 7.1a, so it is refused.
_FIXED_TEXT = "\n".join([*_KEY_FILE_TEXTS, *_STATUS_TEXTS.values(), *_OTHER_TEXTS, API_URL])

Post = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes, Mapping[str, str]]]


# ---------------------------------------------------------------- the key (rules 4.1, 4.2, 4.2a)


def _reveal(credential: object) -> str | None:
    """The key text of a credential (the one place this adapter reveals it), or None when the
    credential is missing or is not a `Redacted` value."""
    return reveal_secret(credential) if isinstance(credential, Redacted) else None


def _shape_problem(key: str) -> bool:
    """True when `key` has an unusable shape or occurs inside a fixed message text."""
    return not _KEY_SHAPE.fullmatch(key) or key in _FIXED_TEXT


def _is_agent_user(uid: int) -> bool:
    """True when the file belongs to the coding agent's account (see `engine.key_file`)."""
    return is_agent_user(uid)


_TEXTS = KeyTexts(_P, NO_KEY, KEY_NOT_USABLE, _FIXED_TEXT)


def read_key_file(path: str | None) -> str:
    """The key in the file at `path`, or the fixed `ProviderError` of rules 4.1 and 4.2.

    The one implementation of rule 4.2 is `engine.key_file.read_key_file`; the agent-user test is
    looked up here at call time, so tests can replace `_is_agent_user` on this module."""
    return _read_key_file(path, _TEXTS, agent=lambda uid: _is_agent_user(uid))


def check_key_file(path: str | None) -> str | None:
    """The fixed problem text for the key file at `path`, or None if usable. Never the key."""
    problem: str | None = None
    key = ""
    try:
        key = read_key_file(path)
    except ProviderError as error:
        problem = error.args[0]
    del key
    return problem


def _hosted() -> tuple[bool, str | None]:
    """The one shared parser (ND220): only `1` is hosted. Returns (hosted, config problem text);
    a bad value is the fixed one-line problem, never guessed. Handlers here only assign."""
    hosted, problem = False, None
    try:
        hosted = hosted_from_env()
    except HostedConfigError:
        problem = HOSTED_FLAG_TEXT
    return hosted, problem


def _key_path() -> str | None:
    return os.environ.get(KEY_FILE_ENV)


def hosted_problem() -> str | None:
    """Only the hosted refusal part of `credential_problem` (a stored key is still never used
    on a hosted server until a hosted secret store exists), or None."""
    hosted, problem = _hosted()
    if problem is not None:
        return problem
    return HOSTED_TEXT if hosted else None


def credential_problem() -> str | None:
    """The fixed problem text for the key of this server (hosted refusal, then the file named by
    `NURAMESH_ANTHROPIC_KEY_FILE`), or None if usable. Never the key or the path. The resolver, the
    probe and the server start all call this one function (spec 4.2a, S9)."""
    hosted, problem = _hosted()
    if problem is not None:
        return problem
    if hosted:
        return HOSTED_TEXT
    return check_key_file(_key_path())


def key_file_provider() -> str:
    """The default key provider: `read_key_file` of the path in `NURAMESH_ANTHROPIC_KEY_FILE`."""
    hosted, problem = _hosted()
    if problem is not None:
        raise ProviderError(problem)
    if hosted:
        raise ProviderError(HOSTED_TEXT)
    return read_key_file(_key_path())


# ---------------------------------------------------------------- connection and probe (11.2, 11.4)


def connection_problem(provider: str, endpoint: str) -> str | None:
    """Why a connection to this provider at `endpoint` is refused, or None. Fixed text."""
    if provider not in KEY_FILE_PROVIDERS:
        return "this provider is not served by the key-file adapter"
    if endpoint != ENDPOINT:
        return f"the endpoint is fixed: {ENDPOINT}"
    return None


@dataclass(frozen=True)
class ProbeFound:
    """What one probe found (the shape of the Azure probe result)."""

    code: str
    status_class: str | None
    latency_ms: int
    called: bool


PROBE_NO_CREDENTIAL = "no usable API key file is available here (set NURAMESH_ANTHROPIC_KEY_FILE)"
PROBE_HOSTED = (
    "no key file is used here: hosted needs a secret store (a later item); "
    "a key file is for self-hosted use only"
)
# The probe texts for this provider (spec 11.4): none says "Entra".
PROBE_TEXTS = {
    "ok": "the API answered and accepted the key",
    "auth_failed": "the API refused the key (HTTP 401 or 403): check the key file",
    "not_found": "the API answered 404 for the probe: check that the key can list models",
    "redirected": "the API tried to redirect the probe; redirects are never followed",
    "server_error": "the API answered with a server error (HTTP 5xx)",
    "unreachable": (
        "the API could not be reached (connection failed or timed out after 10 seconds)"
    ),
}


def probe_text(code: str, status_class: str | None = None) -> str:
    """The fixed text of a probe result for this provider (spec 11.4). For
    `credential_unavailable` it is the bad-flag text, the hosted text or the no-key-file text, in
    that order (the same order as `credential_problem`)."""
    if code == "credential_unavailable":
        hosted, problem = _hosted()
        if problem is not None:
            return HOSTED_ENV_PROBLEM
        return PROBE_HOSTED if hosted else PROBE_NO_CREDENTIAL
    if code == "unexpected_status":
        return f"the API answered with an unexpected status (HTTP {status_class or 'other'})"
    return PROBE_TEXTS[code]


def probe(
    transport: Callable[[str, dict[str, str], float], int],
    credential: CredentialGetter,
    *,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = PROBE_TIMEOUT,
    stored: bool = False,
) -> ProbeFound:
    """One body-less `GET` of the model listing with the key (no tokens, no spend). Never raises;
    no result holds the key, a header or a body. `stored`: the key is a stored model key (R1.14c)
    handed in by the caller, so the key-file setting does not apply."""
    if not stored and credential_problem() is not None:
        return ProbeFound("credential_unavailable", None, 0, False)
    key: str | None = ""
    failed = False
    try:
        key = _reveal(credential())
    except Exception:
        failed = True
    if failed or key is None:
        return ProbeFound("credential_unavailable", None, 0, False)
    headers = {"x-api-key": key, "anthropic-version": API_VERSION, "user-agent": USER_AGENT}
    del key
    started = clock()
    status = 0
    unreachable = False
    try:
        status = transport(PROBE_URL, headers, timeout)
    except Exception:  # assign only: no frame keeps the key after this block
        unreachable = True
    headers.clear()
    latency = max(0, round((clock() - started) * 1000))
    if unreachable:
        return ProbeFound("unreachable", None, latency, True)
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
    return ProbeFound(code, shown, latency, True)


# ---------------------------------------------------------------- transport


def _opener() -> urllib.request.OpenerDirector:
    """An opener that never follows a redirect (built when used, like the Azure adapter)."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
            return None

    # An empty ProxyHandler: proxy environment variables are never read (rule 4.5, T167).
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect, *net_guard.tracked_handlers()
    )


def http_get(url: str, headers: dict[str, str], timeout: float) -> int:
    """The default probe transport: one body-less GET through `_opener` (no proxy, no redirect).
    Returns the status (a 3xx is reported, never followed); any failure is `OSError`."""
    request = urllib.request.Request(url, headers=headers, method="GET")  # noqa: S310
    caught: urllib.error.HTTPError | None = None
    failed = False
    try:
        with _opener().open(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status)  # the body is never read
    except urllib.error.HTTPError as error:
        caught = error
    except Exception:
        failed = True
    if caught is not None:
        caught.close()
        return int(caught.code)
    assert failed
    raise OSError("unreachable")


def _pick(headers: Any) -> dict[str, str]:
    kept: dict[str, str] = {}
    for name in _KEPT_HEADERS:
        value = headers.get(name) if headers is not None else None
        if isinstance(value, str):
            kept[name] = value
    return kept


def _post(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes, Mapping[str, str]]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    caught: urllib.error.HTTPError | None = None
    try:
        with _opener().open(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read(MAX_RESPONSE_BYTES + 1), _pick(response.headers)
    except urllib.error.HTTPError as error:
        caught = error
    assert caught is not None
    kept = _pick(caught.headers)
    caught.close()
    return caught.code, b"", kept  # the body can quote the prompt: never read or kept


# ---------------------------------------------------------------- the provider


@dataclass(frozen=True)
class _Failed:
    message: str


def _shape_problem_of(request: ModelRequest) -> str | None:
    """Rule 3.4: the first failing shape check, or None."""
    system_done = False
    non_system = [m for m in request.messages if m.role != "system"]
    for m in request.messages:
        if m.role != "system":
            system_done = True
        elif system_done:
            return "system messages must come first"
    if not non_system:
        return "needs at least one user message"
    if non_system[0].role != "user":
        return "the first message after system must be from the user"
    if non_system[-1].role != "user":
        return "the last message must be from the user"
    if request.max_output_tokens > MAX_NONSTREAMING_OUTPUT_TOKENS:
        return "max_output_tokens above 16000 needs streaming, which is not supported"
    if any(not m.content for m in non_system):
        return "a message is empty"
    return None


def _backoff(retry_after: str | None, attempt: int) -> float | None:
    """Seconds to wait after attempt `attempt`, or None when the answer says not to retry."""
    if retry_after is not None and _WHOLE_NUMBER.fullmatch(retry_after):
        seconds = int(retry_after)
        if seconds > MAX_RETRY_AFTER:
            return None
        if seconds >= 1:
            return float(seconds)
    return float(2 ** (attempt - 1))


def _count(value: Any, *, optional: bool) -> int | None:
    """A token count from the usage object, or None when it is not valid."""
    if value is None and optional:
        return 0
    if type(value) is not int or value < 0 or value > MAX_TOKENS:
        return None
    return value


def _stage_one(payload: bytes) -> tuple[dict[str, Any] | None, TokenUsage | None, str]:
    """Rule 5.1: the body and its usage. A body that is not a JSON object is a failure text
    (not charged); a readable body with missing or invalid usage returns `usage=None`, which the
    engine charges as the whole reservation with an unknown cost (#1459)."""
    data: Any = None
    unreadable = False
    try:
        data = json.loads(payload)
    except Exception:
        unreadable = True
    if unreadable or not isinstance(data, dict):
        return None, None, "the answer could not be read"
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return data, None, ""
    counts = (
        _count(usage.get("input_tokens"), optional=False),
        _count(usage.get("output_tokens"), optional=False),
        _count(usage.get("cache_read_input_tokens"), optional=True),
        _count(usage.get("cache_creation_input_tokens"), optional=True),
    )
    if any(c is None for c in counts):
        return data, None, ""
    known: Any = counts
    return (
        data,
        TokenUsage(
            input_tokens=known[0],
            output_tokens=known[1],
            cached_input_tokens=known[2],
            cache_write_tokens=known[3],
        ),
        "",
    )


_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")


def _stage_two(data: dict[str, Any], usage: TokenUsage | None) -> ModelResponse:
    """Rule 5.4: the service answered, so this always returns a response (charged; `usage` None
    is charged as the whole reservation, cost unknown)."""
    filtered = ModelResponse(text="", usage=usage, finish_reason="filtered")
    content = data.get("content")
    reason = data.get("stop_reason")
    if data.get("type") != "message" or data.get("role") != "assistant":
        return filtered
    if not isinstance(content, list) or not isinstance(reason, str):
        return filtered
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            return filtered
        kind = block.get("type")
        if kind == "text":
            if not isinstance(block.get("text"), str):
                return filtered
            parts.append(block["text"])
        elif kind not in ("thinking", "redacted_thinking"):
            return filtered
    answer = "".join(parts)
    if len(answer) > MAX_MESSAGE_CHARS or _SURROGATE.search(answer):
        return filtered
    if reason in ("end_turn", "stop_sequence"):
        return ModelResponse(text=answer, usage=usage, finish_reason="stop")
    if reason == "max_tokens":
        return ModelResponse(text=answer, usage=usage, finish_reason="length")
    return filtered


class AnthropicProvider:
    """A `ModelProvider` for the Anthropic Messages API (non-streaming, global, synthetic data)."""

    cache_support: CacheSupport = "explicit"
    """`cache_control` on the system block (R1.34 5.7)."""

    def __init__(
        self,
        key_provider: CredentialGetter,
        *,
        data_class: str,
        processing: str,
        provider_id: str = "anthropic",
        post: Post = _post,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if processing != "global":
            raise ResidencyError(
                "the direct Anthropic API is processed globally; only 'global' is accepted"
            )
        require_data_residency(processing, data_class)
        self._key_provider = key_provider
        self._post = post
        self._sleep = sleep
        self._timeout = timeout
        self._provider_id = provider_id
        self.processing = processing
        self.data_class = data_class
        self._local = threading.local()

    @property
    def attempts(self) -> list[dict[str, Any]]:
        """The attempts of the last call on this thread: `{"n", "outcome"}` (a status or
        `unreachable`), never a header, body or key. The engine records them in `model.called`."""
        found: list[dict[str, Any]] | None = getattr(self._local, "attempts", None)
        if found is None:
            found = self._local.attempts = []
        return found

    def __repr__(self) -> str:
        return f"AnthropicProvider(provider_id={self._provider_id!r})"

    def complete(self, request: ModelRequest) -> ModelResponse:
        result = self._run(request)
        if isinstance(result, _Failed):
            raise ProviderError(result.message)
        return result

    # -- everything that holds the key lives in `_run`; its frame is gone before anything raises

    def _fetch_key(self) -> str | _Failed:
        got: Any = None
        code = 0
        passthrough: str | None = None
        try:
            got = self._key_provider()
        except ProviderError as error:
            code = 1
            passthrough = error.args[0] if error.args else None
        except Exception:
            code = 2
        if code == 1 and isinstance(passthrough, str) and passthrough in _KEY_FILE_TEXTS:
            return _Failed(str(passthrough))
        del passthrough
        secret = _reveal(got)
        del got
        if code != 0 or secret is None:
            return _Failed(KEY_NOT_USABLE)
        if secret == "":
            return _Failed(NO_KEY)
        if _shape_problem(secret):
            return _Failed(KEY_NOT_USABLE)
        return secret

    def _run(self, request: ModelRequest) -> ModelResponse | _Failed:
        if request.provider != self._provider_id:
            return _Failed(f"{_P}this provider only answers its own models")
        problem = _shape_problem_of(request)
        if problem is not None:
            return _Failed(f"{_P}this request cannot be sent to the Messages API ({problem})")
        key = self._fetch_key()
        if isinstance(key, _Failed):
            return key
        result = self._attempts(request, key)
        if isinstance(result, _Failed):
            return _Failed(result.message.replace(key, "[redacted]"))
        return result

    def _attempts(self, request: ModelRequest, key: str) -> ModelResponse | _Failed:
        system = "\n\n".join(
            m.content for m in request.messages if m.role == "system" and m.content
        )
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_output_tokens,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in request.messages
                if m.role != "system"
            ],
            "service_tier": "standard_only",
        }
        if system:
            body["system"] = (
                [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                if request.cache_prefix  # the engine passes True only for status `applied`
                else system
            )
        headers = {
            "x-api-key": key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            "user-agent": USER_AGENT,
        }
        encoded = json.dumps(body).encode("utf-8")
        self.attempts.clear()
        for attempt in range(1, MAX_ATTEMPTS + 1):
            reply: Any = None
            broken = 0
            try:
                if self._post is not _post:
                    call_signal.mark_sent()  # injected transport: sent from here (R20.8)
                reply = self._post(API_URL, headers, encoded, self._timeout)
            except (ValueError, UnicodeError):
                broken = 2
            except Exception:
                broken = 1
            if broken:
                self.attempts.append({"n": attempt, "outcome": "unreachable"})
            if broken == 1:
                return _Failed(f"{_P}the service could not be reached")
            if broken == 2:
                return _Failed(f"{_P}the request could not be sent")
            code, payload, reply_headers = reply
            self.attempts.append({"n": attempt, "outcome": code})
            suffix = _suffix(reply_headers.get("request-id"), key)
            if code == 200:
                return self._parse(payload, suffix, key)
            if 300 <= code < 400:
                return _Failed(
                    _finish(
                        f"{_P}the service tried to redirect; redirects are refused", suffix, key
                    )
                )
            message = _P + _STATUS_TEXTS.get(code, f"the service answered {code}")
            if code in _RETRIED and attempt < MAX_ATTEMPTS:
                wait = _backoff(reply_headers.get("retry-after"), attempt)
                slept = False
                if wait is not None:
                    try:
                        self._sleep(wait)
                        slept = True
                    except Exception:
                        slept = False
                if slept:
                    continue
            return _Failed(_finish(message, suffix, key))
        return _Failed(f"{_P}the service could not be reached")  # pragma: no cover

    def _parse(self, payload: bytes, suffix: str, key: str) -> ModelResponse | _Failed:
        if len(payload) > MAX_RESPONSE_BYTES:
            return _Failed(_finish(f"{_P}the answer was larger than the limit", suffix, key))
        data, usage, failure = _stage_one(payload)
        if data is None or (usage is None and data.get("type") == "error"):
            # an error body without usage is not a served answer: not charged
            return _Failed(_finish(f"{_P}{failure or 'the answer could not be read'}", suffix, key))
        response: ModelResponse | None = None
        try:
            response = _stage_two(data, usage)
        except Exception:
            response = None  # rule 5.4: a billed call never raises; the key frame is not unwound
        if response is None:
            return ModelResponse(text="", usage=usage, finish_reason="filtered")
        return response


def _suffix(request_id: str | None, key: str) -> str:
    """` [request <id>]` for a valid id that does not contain the key (rule 4.4), else empty."""
    if request_id is None or not _REQUEST_ID.fullmatch(request_id) or key in request_id:
        return ""
    return f" [request {request_id}]"


def _finish(message: str, suffix: str, key: str) -> str:
    """The message with its suffix, unless that would contain the key (rule 4.4)."""
    return message if key in message + suffix else message + suffix
