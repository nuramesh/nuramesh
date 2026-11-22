"""The shared key-file reader (spec/r114b 4.1, 4.2, 4.2a, 4.7; spec/openai-compatible 4.3).

Moved out of `engine.anthropic` so a second provider reads key files by the very same rules.
The texts are parametrised by provider (`KeyTexts`); the behaviour is unchanged. A key file is
absolute, outside every repository, a regular file (no link), owned by the server user and not by
the agent user, mode 0400 or 0600, at most 1024 bytes, read on every call and never cached.
`read_key_file` is the only function that returns a key to a caller.
"""

import errno
import logging
import os
import pwd
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass

from nuramesh.core.models import ProviderError

AGENT_USER_ENV = "NURAMESH_AGENT_USER"
DEFAULT_AGENT_USER = "nuramesh-agent"
_ACCOUNT_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


def agent_user_name() -> str:
    """The coding agent's account name: `NURAMESH_AGENT_USER` when it is a plain account name
    ([a-z_][a-z0-9_-]{0,31}), else the neutral default. A set but invalid value (empty, a path,
    an option-like name) falls back to the default and never reaches a lookup."""
    value = os.environ.get(AGENT_USER_ENV)
    if value is not None and _ACCOUNT_NAME.fullmatch(value) is not None:
        return value
    if value:
        # the value itself is never logged (it is untrusted text)
        logging.getLogger(__name__).warning(
            "%s is not a plain account name; using the default %s",
            AGENT_USER_ENV,
            DEFAULT_AGENT_USER,
        )
    return DEFAULT_AGENT_USER


KEY_FILE_MODES = (0o400, 0o600)
MAX_KEY_FILE_BYTES = 1024
FILE_REASONS = (
    "not an absolute path",
    "inside a repository",
    "not a regular file",
    "cannot be read",
    "not owned by the server user",
    "permissions must be 0400 or 0600",
    "readable by the agent user",
    "larger than 1024 bytes",
)
KEY_SHAPE = re.compile(r"[\x21-\x7e]{10,512}")
KEY_SHAPE_BYTES = re.compile(rb"[\x21-\x7e]{10,512}")


@dataclass(frozen=True)
class KeyTexts:
    """The fixed texts one provider says about its key file (never a path, never the key)."""

    prefix: str
    no_key: str
    not_usable: str
    fixed_text: str
    """Every fixed message of the provider joined: a key that occurs inside it is refused."""

    def file_text(self, reason: str) -> str:
        return f"{self.prefix}the API key file is not usable ({reason})"

    def shape_problem(self, key: str) -> bool:
        """True when `key` has an unusable shape or occurs inside a fixed message text."""
        return not KEY_SHAPE.fullmatch(key) or key in self.fixed_text


IsAgentUser = Callable[[int], bool]


def lstat_reason(path: str) -> str:
    """After EACCES: the reason, from one `lstat` for the error text only (rule 4.2 step 3a)."""
    found = None
    try:
        found = os.lstat(path)
    except OSError:
        pass
    if found is None:
        return "cannot be read"
    if not stat.S_ISREG(found.st_mode):
        return "not a regular file"
    if found.st_uid != os.geteuid():
        return "not owned by the server user"
    if stat.S_IMODE(found.st_mode) not in KEY_FILE_MODES:
        return "permissions must be 0400 or 0600"
    return "cannot be read"


def is_agent_user(uid: int) -> bool:
    """True when the file belongs to the coding agent's account (no group or other bit is set, so
    the owner is the only reader)."""
    agent_uid: int | None = None
    try:
        agent_uid = pwd.getpwnam(agent_user_name()).pw_uid
    except (KeyError, OSError):
        pass
    return agent_uid == uid


def in_repository(path: str) -> bool:
    folder = os.path.dirname(os.path.realpath(path))
    while True:
        if os.path.lexists(os.path.join(folder, ".git")):
            return True
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent


def open_key(path: str) -> tuple[int, str | None]:
    """Open without following a link or blocking; returns `(fd, None)` or `(-1, reason)`."""
    code = 0
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        code = error.errno or -1
    if fd >= 0:
        return fd, None
    if code == errno.ELOOP:
        return -1, "not a regular file"
    if code == errno.EACCES:
        return -1, lstat_reason(path)
    return -1, "cannot be read"


def _read_fd(fd: int, agent: IsAgentUser, limit: int) -> tuple[bytes, str | None]:
    """Every check on the open descriptor (rule 4.2 steps 4 to 7); closes it on every path."""
    data = b""
    reason: str | None = None
    failed = False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            reason = "not a regular file"
        elif info.st_uid != os.geteuid():
            reason = "not owned by the server user"
        elif stat.S_IMODE(info.st_mode) not in KEY_FILE_MODES:
            reason = "permissions must be 0400 or 0600"
        elif agent(info.st_uid):
            reason = "readable by the agent user"
        elif info.st_size > limit:
            reason = f"larger than {limit} bytes"
        else:
            while len(data) <= limit:
                chunk = os.read(fd, limit + 1 - len(data))
                if not chunk:
                    break
                data += chunk
            if len(data) > limit:
                reason = f"larger than {limit} bytes"
    except OSError:
        failed = True
    try:
        os.close(fd)
    except OSError:
        pass
    if failed:
        return b"", "cannot be read"
    return data, reason


def read_checked_file(
    path: str | None,
    texts: KeyTexts,
    *,
    agent: IsAgentUser = is_agent_user,
    limit: int = MAX_KEY_FILE_BYTES,
) -> bytes:
    """The bytes of the file at `path` after every file check of rule 4.2, or the fixed
    `ProviderError`. Used for key files (1024 bytes) and the operator binding file (16384).

    The one implementation of rule 4.2; the only function that returns the key to a caller."""
    if not path:
        raise ProviderError(texts.no_key)
    if "\0" in path:
        raise ProviderError(texts.file_text("cannot be read"))
    if not os.path.isabs(path):
        raise ProviderError(texts.file_text("not an absolute path"))
    if in_repository(path):
        raise ProviderError(texts.file_text("inside a repository"))
    fd, reason = open_key(path)
    data = b""
    if reason is None:
        data, reason = _read_fd(fd, agent, limit)
    if reason is not None:
        raise ProviderError(texts.file_text(reason))
    return data


def read_key_file(path: str | None, texts: KeyTexts, *, agent: IsAgentUser = is_agent_user) -> str:
    """The key in the file at `path`, or the fixed `ProviderError` of rules 4.1 and 4.2.

    The one implementation of rule 4.2; the only function that returns the key to a caller."""
    data = read_checked_file(path, texts, agent=agent)
    if data.endswith(b"\r\n"):
        data = data[:-2]
    elif data.endswith(b"\n"):
        data = data[:-1]
    if not data:
        raise ProviderError(texts.no_key)
    if not KEY_SHAPE_BYTES.fullmatch(data):
        raise ProviderError(texts.not_usable)
    key = data.decode("ascii")
    if texts.shape_problem(key):
        raise ProviderError(texts.not_usable)
    return key
