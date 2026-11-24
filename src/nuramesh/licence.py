"""The licence loader: the trusted public keys pinned in
the open core, the file named by `NURAMESH_LICENCE_FILE`, the Ed25519 check from `cryptography`
(imported lazily, like anchors) and one report. Everything about the format and the rules is in
`nuramesh.core.licence`; this module only wires it to the process.

Offline by design (ND72): nothing here opens a network connection, and there is no flag or
environment value that points the verifier at another key table. A refused file never raises:
paid features stay off and the open core runs exactly as with no file at all.
"""

import errno
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from nuramesh.core.entitlements import LicenceEntitlements
from nuramesh.core.licence import (
    MAX_LICENCE_BYTES,
    LicenceClaims,
    SignatureCheck,
    licence_key_id,
    verify_licence,
)
from nuramesh.hosted import HostedConfigError, hosted_from_env

LICENCE_ENV: Final = "NURAMESH_LICENCE_FILE"

# (key id, the 32 raw public key bytes as 64 hex). A public key is not a secret (ND389). EMPTY
# until the owner's signing key exists (Azure Key Vault, not built here): every real file is
# `unknown_key_id` and nothing can be granted. Adding, rotating or removing a row is a tier-3 code
# change; removal is the only revocation an offline verifier has.
TRUSTED_KEYS: tuple[tuple[str, str], ...] = ()

_STAMP: Final = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class LicenceReport:
    """What the loader found: for the CLI's one stderr line and for later status commands."""

    configured: bool
    problem: str | None
    detail: str
    licence_id: str | None = None
    key_id: str | None = None
    expires_at: datetime | None = None
    granted: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()

    @property
    def notice(self) -> str | None:
        """The one line to print, or None when there is nothing to say."""
        if self.problem is None:
            return None
        return f"licence: {self.problem}: {self.detail}"


UNCONFIGURED: Final = LicenceReport(configured=False, problem=None, detail="")


def trusted_table() -> dict[str, bytes]:
    """The pinned keys by id; a row whose id is not the id of its key is a programming error."""
    table: dict[str, bytes] = {}
    for key_id, raw_hex in TRUSTED_KEYS:
        raw = bytes.fromhex(raw_hex)
        if len(raw) != 32 or licence_key_id(raw) != key_id:
            raise ValueError(f"trusted licence key {key_id} does not match its bytes")
        table[key_id] = raw
    return table


def _signature_check() -> SignatureCheck | None:
    """The Ed25519 check, or None when `cryptography` is not installed."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return None

    def check(raw: bytes, message: bytes, signature: bytes) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(raw).verify(signature, message)
        except (InvalidSignature, ValueError):
            return False
        return True

    return check


def _refused(problem: str, detail: str, claims: LicenceClaims | None = None) -> LicenceReport:
    if claims is None:
        return LicenceReport(configured=True, problem=problem, detail=detail)
    return LicenceReport(
        configured=True,
        problem=problem,
        detail=detail,
        licence_id=claims.licence_id,
        key_id=claims.key_id,
        expires_at=claims.expires_at,
    )


def _read_capped(path_text: str) -> bytes:
    """Read at most one byte past the cap. The file is opened non-blocking and a non-regular
    file (FIFO, device, directory, socket) is refused before any read, so a bad path can neither
    hang startup nor grow memory. Symlinks are followed (secret mounts use them)."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path_text, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_LICENCE_BYTES + 1)


def load_licence(
    env: Mapping[str, str] | None = None, clock: Callable[[], datetime] | None = None
) -> tuple[LicenceReport, LicenceEntitlements | None]:
    """Read and verify the configured licence file. The second value is the entitlement source
    for a valid file and None otherwise; the report says why."""
    mapping: Mapping[str, str] = os.environ if env is None else env
    path_text = mapping.get(LICENCE_ENV, "")
    if path_text == "":
        return UNCONFIGURED, None
    try:
        hosted = hosted_from_env(mapping)
    except HostedConfigError as exc:
        return _refused("refused_hosted", str(exc)), None
    if hosted:
        return _refused("refused_hosted", "a licence file is not read in a hosted deployment"), None
    try:
        data = _read_capped(path_text)
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        return _refused("unreadable", f"{LICENCE_ENV}: {reason}"), None
    check = _signature_check()
    if check is None:
        detail = (
            "the 'cryptography' package is not installed (nuramesh[server] or nuramesh[anchors])"
        )
        return _refused("verifier_unavailable", detail), None
    now = clock if clock is not None else _utc_now
    result = verify_licence(data, trusted_table(), check, now())
    if not result.ok:
        return _refused(result.problem or "malformed", result.detail, result.claims), None
    claims = result.claims
    assert claims is not None  # a valid result always carries them
    source = LicenceEntitlements(claims.features, claims.not_before, claims.expires_at, now)
    report = LicenceReport(
        configured=True,
        problem=None,
        detail="",
        licence_id=claims.licence_id,
        key_id=claims.key_id,
        expires_at=claims.expires_at,
        granted=tuple(sorted(source.granted)),
        ignored=tuple(sorted(source.ignored)),
    )
    return report, source


def _utc_now() -> datetime:
    return datetime.now(UTC)
