"""The sandbox service's rules as pure functions (real-models spec section 7; PR3a of #963).

The service (`nuramesh.sandboxd`) and its client both use these; nothing here does I/O.

- **State and caps (R52, R53):** `service_state` turns the text of the service's own cgroup files
  and of `/proc/self/uid_map` into `ready` or one of four refusal states, first failure wins:
  `sandbox_rootful` (the user namespace is the host's: an escape would be a real host user),
  `sandbox_unlimited` (no finite memory, CPU or process limit, or swap allowed),
  `sandbox_caps_too_small`, `sandbox_selftest_failed`. Whatever cannot be read or parsed counts
  as the unsafe case: the service then runs nothing.
- **Admission (R54):** `admit` gives the one code a request gets, first failure wins. A limit
  above a cap is refused, never lowered.
- **Frames (7.5):** one request per connection, a 4-byte length and strict JSON. Unknown fields,
  another version, a duplicate key or trailing bytes are refused.
- **Workspace archive (R57):** `ArchiveChecker` applies the member rules one entry at a time, on
  the effective name; the unpacker creates a member only after its entry was accepted.
"""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final, Literal

from pydantic import ValidationError

from nuramesh.core.sandbox import (
    SERVICE_RUNTIME,
    Isolation,
    SandboxLimits,
    SandboxRequest,
    SandboxResult,
)

WIRE_VERSION = 1
MAX_REQUEST_JSON = 65_536
MAX_RESPONSE_JSON = 2_097_152
MAX_OUTPUT_BYTES = 131_072  # R54.3: the most a request may ask for in the service
MAX_ARCHIVE_MEMBERS = 20_000
RESERVED_MEMORY_MB = 64  # R53: subtracted for the supervisor
RESERVED_PIDS = 16

READY = "ready"
OK = "ok"
STATE_CODES = (
    "sandbox_rootful",
    "sandbox_unlimited",
    "sandbox_caps_too_small",
    "sandbox_selftest_failed",
)
REFUSAL_CODES = (
    *STATE_CODES,
    "sandbox_bad_request",
    "sandbox_limit_too_high",
    "sandbox_image_missing",
    "sandbox_workspace_too_large",
    "sandbox_workspace_refused",
)

Op = Literal["run", "health"]


class WireError(ValueError):
    """A frame that is not valid; `code` is always `sandbox_bad_request`."""

    code = "sandbox_bad_request"


# --- state and caps ------------------------------------------------------------------------------


@dataclass(frozen=True)
class CgroupFiles:
    """The text of the service's own cgroup files; `None` is missing or unreadable."""

    memory_max: str | None
    cpu_max: str | None
    pids_max: str | None
    memory_swap_max: str | None = "0"
    swap_file_exists: bool = True  # False: the kernel has no swap accounting, which passes


@dataclass(frozen=True)
class Caps:
    """What one command may ask for at most (R53)."""

    memory_mb: int
    cpu_quota: int
    cpu_period: int
    pids: int

    def allows_cpus(self, cpus: Decimal) -> bool:
        """R54.3, exact: `cpus * period <= quota`."""
        return Fraction(cpus) * self.cpu_period <= self.cpu_quota

    def cpus_text(self) -> str:
        """`quota / period` as a decimal when exact, else as a reduced fraction."""
        value = Fraction(self.cpu_quota, self.cpu_period)
        rest = value.denominator
        for prime in (2, 5):
            while rest % prime == 0:
                rest //= prime
        if rest != 1:
            return f"{value.numerator}/{value.denominator}"
        text = format(Decimal(value.numerator) / Decimal(value.denominator), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    @property
    def too_small(self) -> bool:
        """A cap of 0 or below."""
        return self.memory_mb <= 0 or self.cpu_quota <= 0 or self.pids <= 0


@dataclass(frozen=True)
class ServiceState:
    """`ready` or a state code, and the caps when the cgroup limits are finite."""

    code: str
    caps: Caps | None

    @property
    def ready(self) -> bool:
        """The service may run a command."""
        return self.code == READY


def _count(text: str | None) -> int | None:
    """A non-negative decimal integer, or None (`max`, missing, anything else)."""
    if text is None:
        return None
    word = text.strip()
    if not word.isascii() or not word.isdigit():
        return None
    return int(word)


def uid_map_is_rootful(text: str | None) -> bool:
    """True when the service's user namespace reaches host user id 0, or cannot be told.

    `text` is `/proc/self/uid_map`: lines of `inside outside count`. In the host's own user
    namespace (rootful Docker, Colima, Docker Desktop: `0 0 4294967295`) a process that escaped
    the container would be a real host user, so the service refuses to run anything
    (`sandbox_rootful`, OQ3 option (b)). Fields are parsed, text is never compared (the kernel
    pads with spaces). Any line whose outside range starts at 0 is rootful; an empty, missing or
    unparsable map is rootful too: user namespace protection is never assumed.
    """
    if text is None:
        return True
    lines = [line.split() for line in text.splitlines() if line.strip()]
    if not lines:
        return True
    for fields in lines:
        if len(fields) != 3:
            return True
        numbers = [_count(f) for f in fields]
        if any(n is None for n in numbers):
            return True
        _inside, outside, count = numbers
        if count == 0 or outside == 0:
            return True
    return False


def _caps(files: CgroupFiles) -> Caps | None:
    memory = _count(files.memory_max)
    pids = _count(files.pids_max)
    cpu = (files.cpu_max or "").split()
    quota = _count(cpu[0]) if len(cpu) == 2 else None
    period = _count(cpu[1]) if len(cpu) == 2 else None
    if memory is None or pids is None or quota is None or not period:
        return None
    if files.swap_file_exists and _count(files.memory_swap_max) != 0:
        return None
    return Caps(
        memory_mb=memory // 1_048_576 - RESERVED_MEMORY_MB,
        cpu_quota=quota,
        cpu_period=period,
        pids=pids - RESERVED_PIDS,
    )


def status_field(text: str | None, name: str, *, base: int = 10) -> int | None:
    """One integer field of a `/proc/<pid>/status` text; None when missing, twice or no number."""
    values = [
        line.split(":", 1)[1].strip()
        for line in (text or "").splitlines()
        if line.split(":", 1)[0] == name and ":" in line
    ]
    if len(values) != 1:
        return None
    try:
        return int(values[0], base)
    except ValueError:
        return None


def service_state(
    files: CgroupFiles,
    uid_map: str | None,
    *,
    selftest: Callable[[], bool],
    supervisor_status: str | None,
) -> ServiceState:
    """The state of R52. `selftest` runs only when every earlier check passed.

    `supervisor_status` is the supervisor's own `/proc/self/status`: without `Seccomp` 2 no
    profile is loaded in the container (`seccomp=unconfined`), the command would run under its
    own deny-list only, and the state is `sandbox_selftest_failed`.
    """
    caps = _caps(files)
    if uid_map_is_rootful(uid_map):
        return ServiceState("sandbox_rootful", caps)
    if status_field(supervisor_status, "Seccomp") != 2:
        return ServiceState("sandbox_selftest_failed", caps)
    if caps is None:
        return ServiceState("sandbox_unlimited", None)
    if caps.too_small:
        return ServiceState("sandbox_caps_too_small", caps)
    try:
        passed = selftest() is True
    except Exception:  # a self-test that cannot be made is a failed self-test
        passed = False
    return ServiceState(READY if passed else "sandbox_selftest_failed", caps)


def admit(
    state: ServiceState,
    *,
    frame_valid: bool = True,
    limits: SandboxLimits,
    image_in_store: bool,
    workspace_bytes: int,
    workspace_max_mb: int,
    archive_ok: bool = True,
) -> str:
    """The code of a `run` request (R54): `ok` or the first refusal."""
    if not frame_valid:
        return "sandbox_bad_request"
    if not state.ready or state.caps is None:
        return state.code if not state.ready else "sandbox_unlimited"
    caps = state.caps
    if (
        limits.memory_mb > caps.memory_mb
        or not caps.allows_cpus(limits.cpus)
        or limits.pids > caps.pids
        or limits.output_bytes > MAX_OUTPUT_BYTES
    ):
        return "sandbox_limit_too_high"
    if not image_in_store:
        return "sandbox_image_missing"
    if workspace_bytes > workspace_max_mb * 1_048_576:
        return "sandbox_workspace_too_large"
    if not archive_ok:
        return "sandbox_workspace_refused"
    return OK


# --- frames --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RequestOp:
    """A decoded request: `health`, or `run` with its request and the archive length."""

    op: Op
    request: SandboxRequest | None = None
    workspace_bytes: int = 0


def _length(prefix: bytes, most: int) -> int:
    if len(prefix) != 4:
        raise WireError("a frame starts with a 4-byte length")
    n = int.from_bytes(prefix, "big")
    if not 1 <= n <= most:
        raise WireError("the frame length is out of range")
    return n


def request_length(prefix: bytes) -> int:
    """The JSON length of a request frame (1 to 65,536)."""
    return _length(prefix, MAX_REQUEST_JSON)


def response_length(prefix: bytes) -> int:
    """The JSON length of a response frame (1 to 2,097,152)."""
    return _length(prefix, MAX_RESPONSE_JSON)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise WireError("a key is given twice")
        out[key] = value
    return out


def _refuse_constant(_name: str) -> Any:
    raise WireError("not a JSON number")


def _document(payload: bytes, keys: frozenset[str]) -> dict[str, Any]:
    try:
        doc = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_refuse_constant,
        )
    except (ValueError, RecursionError) as error:  # ValueError: also an integer over 4300 digits
        raise WireError("the frame is not UTF-8 JSON") from error
    if not isinstance(doc, dict) or set(doc) != keys:
        raise WireError("the frame has missing or unknown fields")
    if type(doc["v"]) is not int or doc["v"] != WIRE_VERSION:
        raise WireError("another wire version")
    return doc


def decode_request(payload: bytes) -> RequestOp:
    """Decode the JSON of a request frame; anything unexpected is a `WireError`."""
    try:
        peek = json.loads(payload.decode("utf-8"), parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as error:  # ValueError: also an integer over 4300 digits
        raise WireError("the frame is not UTF-8 JSON") from error
    op = peek.get("op") if isinstance(peek, dict) else None
    if op == "health":
        _document(payload, frozenset({"v", "op"}))
        return RequestOp("health")
    if op != "run":
        raise WireError("unknown op")
    doc = _document(payload, frozenset({"v", "op", "request", "workspace_bytes"}))
    size = doc["workspace_bytes"]
    if type(size) is not int or size < 0:
        raise WireError("workspace_bytes must be a non-negative integer")
    try:
        request = SandboxRequest.model_validate(doc["request"])
    except ValidationError as error:
        raise WireError("the request is not a valid SandboxRequest") from error
    return RequestOp("run", request, size)


def _frame(doc: Mapping[str, Any], most: int) -> bytes:
    body = json.dumps(doc, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if not 1 <= len(body) <= most:
        raise WireError("the frame is too long")
    return len(body).to_bytes(4, "big") + body


def encode_request(
    op: Op, request: SandboxRequest | None = None, *, workspace_bytes: int = 0
) -> bytes:
    """A request frame. A request whose JSON is over 65,536 bytes is refused before sending."""
    if op == "health":
        return _frame({"v": WIRE_VERSION, "op": "health"}, MAX_REQUEST_JSON)
    if request is None or workspace_bytes < 0:
        raise WireError("a run request needs a request and an archive length")
    return _frame(
        {
            "v": WIRE_VERSION,
            "op": "run",
            "request": request.model_dump(mode="json"),
            "workspace_bytes": workspace_bytes,
        },
        MAX_REQUEST_JSON,
    )


def encode_response(code: str, result: SandboxResult | None) -> bytes:
    """A response frame: `result` exactly when `code` is `ok`."""
    if code not in (OK, READY, *REFUSAL_CODES) or (code == OK) != (result is not None):
        raise WireError("not a valid response")
    body = None if result is None else result.model_dump(mode="json")
    return _frame({"v": WIRE_VERSION, "code": code, "result": body}, MAX_RESPONSE_JSON)


def decode_response(
    payload: bytes, *, op: Op, output_bytes: int
) -> tuple[str, SandboxResult | None]:
    """Decode and check the JSON of a response to `op` (the runner's strict check, R55)."""
    doc = _document(payload, frozenset({"v", "code", "result"}))
    code = doc["code"]
    allowed = (READY, *STATE_CODES) if op == "health" else (OK, *REFUSAL_CODES)
    if not isinstance(code, str) or code not in allowed:
        raise WireError("a code this request cannot get")
    if (code == OK) != (doc["result"] is not None):
        raise WireError("a result is present exactly when the code is ok")
    if doc["result"] is None:
        return code, None
    try:
        result = SandboxResult.model_validate(doc["result"])
    except ValidationError as error:
        raise WireError("the result is not a valid SandboxResult") from error
    if result.isolation is not Isolation.SERVICE or result.runtime != SERVICE_RUNTIME:
        raise WireError("the result is not from the sandbox service")
    if result.cancelled:
        raise WireError("the service cannot say a command was cancelled")
    if result.workspace_sha256 is None:
        raise WireError("an ok result must carry the archive hash")
    if any(len(t.encode("utf-8")) > output_bytes for t in (result.stdout_tail, result.stderr_tail)):
        raise WireError("a tail is longer than the request allows")
    return code, result


def cut_tail(data: bytes, output_bytes: int) -> str:
    """The tail of an output (7.5): decode with replacement, then cut whole characters from the
    front until the UTF-8 length is at most `output_bytes`."""
    text = data.decode("utf-8", "replace")
    encoded = text.encode("utf-8")
    if len(encoded) <= output_bytes:
        return text
    start = len(encoded) - output_bytes
    while start < len(encoded) and (encoded[start] & 0xC0) == 0x80:
        start += 1  # not in the middle of a character
    return encoded[start:].decode("utf-8")


# --- workspace archive ---------------------------------------------------------------------------


class ArchiveRefused(ValueError):
    """An archive member breaks a rule of R57 (`sandbox_workspace_refused`)."""


@dataclass(frozen=True)
class ArchiveEntry:
    """One archive member: its name after PAX and long-name records, its kind and size.

    `kind` is `file`, `dir` or `symlink`; every other word (`hardlink`, `chardev`, `blockdev`,
    `fifo`, `socket`, `sparse`, ...) is refused.
    """

    name: str
    kind: str
    size: int
    mode: int = 0o644


def effective_name(name: str) -> str:
    """R57: drop leading `./`, join repeated `/`, drop a trailing `/`; refuse the rest."""
    if "\x00" in name:
        raise ArchiveRefused("a name with NUL")
    if name.startswith("/"):
        raise ArchiveRefused("an absolute path")
    parts = [p for p in name.split("/") if p != ""]
    while parts and parts[0] == ".":
        parts.pop(0)
    if not parts:
        raise ArchiveRefused("an empty name")
    if any(p in (".", "..") for p in parts):
        raise ArchiveRefused("a '.' or '..' path part")
    return "/".join(parts)


class ArchiveChecker:
    """Applies R57 entry by entry; `add` returns the entry to create or raises `ArchiveRefused`."""

    def __init__(self, limit_bytes: int) -> None:
        self._limit = limit_bytes
        self._kinds: dict[str, str] = {}
        self._parents: set[str] = set()
        self._bytes = 0

    def add(self, entry: ArchiveEntry) -> ArchiveEntry:
        """Check one member against everything seen so far."""
        if entry.kind not in ("file", "dir", "symlink"):
            raise ArchiveRefused(f"a member of kind '{entry.kind[:20]}'")
        name = effective_name(entry.name)
        if len(self._kinds) >= MAX_ARCHIVE_MEMBERS:
            raise ArchiveRefused("too many members")
        if name in self._kinds:
            raise ArchiveRefused("two members with the same name")
        if entry.size < 0:
            raise ArchiveRefused("a negative size")
        if name in self._parents and entry.kind != "dir":
            raise ArchiveRefused("a path goes through a member that is not a directory")
        parts = name.split("/")
        parents = ["/".join(parts[:i]) for i in range(1, len(parts))]
        if any(self._kinds.get(p, "dir") != "dir" for p in parents):
            raise ArchiveRefused("a path goes through a link or a file of the archive")
        if entry.kind == "file":
            self._bytes += entry.size
            if self._bytes > self._limit:
                raise ArchiveRefused("the files are larger than the limit")
        self._kinds[name] = entry.kind
        self._parents.update(parents)
        # set-uid, set-gid and sticky bits are dropped
        return ArchiveEntry(name, entry.kind, entry.size, entry.mode & 0o777)


# --- which sandbox a runner uses (R40, R41) --------------------------------------------------

SERVICE: Final = "service"
HOST: Final = "host"
NONE: Final = "none"
HOST_RUNTIMES: Final = ("podman", "docker")  # Podman first, then Docker (R40.2)
NO_ANSWER: Final = "no answer"  # a health probe that got no valid answer in 5 s (R52)
PROBE_SECONDS: Final = 5
PROBE_MAX_AGE_SECONDS: Final = 30


@dataclass(frozen=True)
class RunnerSandboxChoice:
    """The runner's choice at its start: `kind` is `service`, `host` or `none`; `runtime` is set
    for `host`; `exit_code` 2 (and no kind) when the runner must not start."""

    kind: str | None
    runtime: str | None = None
    exit_code: int | None = None


def choose_runner_sandbox(
    socket_value: str | None, host_runtimes: Sequence[str]
) -> RunnerSandboxChoice:
    """R40. `socket_value` is `NURAMESH_SANDBOX_SOCKET` (None: not set); `host_runtimes` the
    working container runtimes found on the host. A set socket never falls back to the host; the
    unsandboxed opt-out does not exist here."""
    if socket_value:
        if not socket_value.startswith("/"):
            return RunnerSandboxChoice(kind=None, exit_code=2)
        return RunnerSandboxChoice(kind=SERVICE)
    for name in HOST_RUNTIMES:
        if name in host_runtimes:
            return RunnerSandboxChoice(kind=HOST, runtime=name)
    return RunnerSandboxChoice(kind=NONE)


def runner_sandbox_ready(
    kind: str | None, *, last_probe: str | None = None, runtime_found: bool = False
) -> bool:
    """R41: service: the last health probe answered `ready`; host: a runtime was found at start;
    none (and anything else): never."""
    if kind == SERVICE:
        return last_probe == READY
    return kind == HOST and runtime_found
