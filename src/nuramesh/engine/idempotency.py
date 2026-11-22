"""Sealing the stored response of an idempotency key (R1.35, section 6.3).

The response is encrypted with a key derived from the organization's content key (the existing
key ring): AES-256-GCM, a fresh nonce per seal, the scope, key hash, fingerprint and status as
associated data. A key is made only by a successful first store (`seal`), never by a read.
"""

import os
from collections.abc import Mapping

from nuramesh.core.content import hkdf, key_id
from nuramesh.core.idempotency import (
    SEAL_INFO,
    Scope,
    Sealed,
    associated_data,
    pack_response,
    unpack_response,
)
from nuramesh.engine.content import ContentError, ContentService, _aesgcm
from nuramesh.stores.base import UnitOfWork

NONCE_BYTES = 12


class SealUnavailable(Exception):
    """The ring, the `content` extra or the key cannot be used right now (503
    `idempotency_unavailable`): nothing is stored."""


class ResponseUnavailable(Exception):
    """A stored response cannot be opened (409 `idempotency_response_unavailable`): the key was
    destroyed or changed, or the row failed authentication. The route is not run."""


def _idem_key(master: bytes) -> bytes:
    return hkdf(master, SEAL_INFO)


class ResponseSealer:
    """Seals and opens stored responses with the organization's key from `ContentService`."""

    def __init__(self, content: ContentService) -> None:
        self.content = content

    def check_ring(self, ring_org: str) -> None:
        """Step 4.5: the ring can be read (never creating a key) and the extra is installed. A
        tombstone is a clean absence here (the caller notes it separately); anything that raises
        is `SealUnavailable`."""
        try:
            self.content._master_for_use(ring_org)
            _aesgcm(bytes(32))
        except (ContentError, OSError, ValueError) as exc:
            raise SealUnavailable("the key ring cannot be used") from exc

    def tombstoned(self, ring_org: str) -> bool:
        """Whether the organization's key was destroyed (either tombstone copy)."""
        try:
            return self.content.retired(ring_org) is not None
        except (ContentError, OSError) as exc:
            raise SealUnavailable("the tombstone cannot be read") from exc

    def seal(
        self,
        uow: UnitOfWork,
        ring_org: str,
        scope: Scope,
        key_hash: str,
        fingerprint: str,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Sealed:
        """Encrypt a response. Makes the organization's key only here, through the capture gate
        (`_master_for_capture`), which refuses a destroyed one. Any failure is `SealUnavailable`."""
        try:
            master = self.content._master_for_capture(ring_org, uow)
            cipher = _aesgcm(_idem_key(master))
            nonce = os.urandom(NONCE_BYTES)
            payload = cipher.encrypt(  # type: ignore[attr-defined]
                nonce,
                pack_response(headers, body),
                associated_data(scope, key_hash, fingerprint, status),
            )
            return Sealed(bytes(payload), nonce, key_id(master))
        except (ContentError, OSError, ValueError) as exc:
            raise SealUnavailable("the response cannot be sealed") from exc

    def open(
        self,
        ring_org: str,
        scope: Scope,
        key_hash: str,
        fingerprint: str,
        status: int,
        sealed: Sealed,
    ) -> tuple[dict[str, str], bytes]:
        """Decrypt a stored response. A ring that raises is `SealUnavailable`; a missing key, a
        different `key_id` or a failed authentication is `ResponseUnavailable`. Makes no key."""
        try:
            master = self.content._master_for_use(ring_org)
        except (ContentError, OSError) as exc:
            raise SealUnavailable("the key ring cannot be read") from exc
        if master is None or key_id(master) != sealed.key_id:
            raise ResponseUnavailable("the response was sealed under another key")
        try:
            cipher = _aesgcm(_idem_key(master))
            plain = cipher.decrypt(  # type: ignore[attr-defined]
                sealed.nonce,
                sealed.payload,
                associated_data(scope, key_hash, fingerprint, status),
            )
            return unpack_response(bytes(plain))
        except ContentError as exc:  # the extra is missing
            raise SealUnavailable("the key ring cannot be used") from exc
        except Exception as exc:  # cryptography raises InvalidTag, whose type we don't import
            raise ResponseUnavailable("the stored response failed its integrity check") from exc
