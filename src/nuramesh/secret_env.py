"""Secrets that may come from a file (ND423, #896 slice 2): `NAME` holds the value, `NAME_FILE`
holds the path of a file with the value. Setting both is an error; so is a file that group or
others can read, is missing, unreadable, empty or not a regular file. One trailing newline is
dropped. A symlink to a file is followed (Kubernetes mounts secrets that way); the mode
checked is the target's. The messages name the variable (for the mode, also the path the
operator gave) and never the value. Stdlib only, so any layer may import it."""

import os
import stat
from collections.abc import Mapping

_MAX_BYTES = 64 * 1024
DATABASE_URL = "NURAMESH_DATABASE_URL"
REFRESH_SUCCESSOR_KEY = "NURAMESH_REFRESH_SUCCESSOR_KEY"
OTEL_HEADERS = "NURAMESH_OTEL_HEADERS"
FILE_TWINS = (DATABASE_URL, REFRESH_SUCCESSOR_KEY, OTEL_HEADERS)
# The twin names spelled out (a docs test greps the code for every name it documents).
TWIN_NAMES = (
    "NURAMESH_DATABASE_URL_FILE",
    "NURAMESH_REFRESH_SUCCESSOR_KEY_FILE",
    "NURAMESH_OTEL_HEADERS_FILE",
)


class SecretEnvError(ValueError):
    """A secret setting is unusable. One line naming the variable, never the value."""

    def __init__(self, name: str, reason: str) -> None:
        self.name = name
        super().__init__(f"{name}: {reason}")


def read_secret(name: str, env: Mapping[str, str] | None = None) -> str | None:
    """The value of `name`, read from `name` or from the file named by `name_FILE`; None when
    neither is set (an empty value counts as not set). `SecretEnvError` for both set, or for an
    unsafe or empty file."""
    source = os.environ if env is None else env
    twin = f"{name}_FILE"
    value = source.get(name)
    path = source.get(twin)
    if path is None or not path.strip():  # an empty twin counts as not set
        return value
    if value is not None and value.strip():
        raise SecretEnvError(name, f"set both {name} and {twin}: use one")
    return _read_file(twin, path)


def _read_file(twin: str, path: str) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        raise SecretEnvError(twin, "the file is missing or unreadable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SecretEnvError(twin, "must name a regular file")
        if info.st_mode & 0o077:
            raise SecretEnvError(
                twin,
                f"{path} can be read by group or others: run `chmod 600 {path}` and start again",
            )
        data = os.read(fd, _MAX_BYTES + 1)
    except OSError:
        raise SecretEnvError(twin, "the file is missing or unreadable") from None
    finally:
        os.close(fd)
    if len(data) > _MAX_BYTES:
        raise SecretEnvError(twin, "the file is too large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretEnvError(twin, "the file is not text") from None
    text = text.removesuffix("\n")
    if not text.strip():
        raise SecretEnvError(twin, "the file is empty")
    return text


def database_url(env: Mapping[str, str] | None = None) -> str | None:
    """`NURAMESH_DATABASE_URL` or its `_FILE` twin (empty text counts as unset, as before)."""
    return read_secret(DATABASE_URL, env) or None


def startup_problem(env: Mapping[str, str] | None = None) -> str | None:
    """One line for the first unusable secret setting, or None."""
    for name in FILE_TWINS:
        try:
            read_secret(name, env)
        except SecretEnvError as exc:
            return str(exc)
    return None
