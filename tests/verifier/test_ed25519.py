"""The verifier's own Ed25519 check (RFC 8032 section 5.1.7) against known answers and OpenSSL."""

import os
import random

import pytest
from verify_helpers import load_verifier

verifier = load_verifier()

# RFC 8032 section 7.1, tests 1 and 2: (public key, message, signature), all hex.
RFC = [
    (
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        "",
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b",
    ),
    (
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
        "72",
        "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00",
    ),
]


@pytest.mark.parametrize(("public", "message", "signature"), RFC)
def test_rfc_vectors_verify_and_a_changed_message_does_not(
    public: str, message: str, signature: str
) -> None:
    pk, msg, sig = bytes.fromhex(public), bytes.fromhex(message), bytes.fromhex(signature)
    assert verifier.ed25519_verify(pk, msg, sig)
    assert not verifier.ed25519_verify(pk, msg + b"x", sig)


def test_agrees_with_openssl_on_random_and_corrupted_signatures() -> None:
    ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization

    rng = random.Random(1)
    for _ in range(12):
        key = ed.Ed25519PrivateKey.from_private_bytes(os.urandom(32))
        public = key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        message = rng.randbytes(rng.randrange(0, 200))
        signature = key.sign(message)
        assert verifier.ed25519_verify(public, message, signature)
        bad = bytearray(signature)
        bad[rng.randrange(64)] ^= 1 << rng.randrange(8)
        try:
            key.public_key().verify(bytes(bad), message)
            expected = True
        except InvalidSignature:
            expected = False
        assert verifier.ed25519_verify(public, message, bytes(bad)) is expected


def test_rejects_a_scalar_not_below_the_group_order_and_bad_lengths() -> None:
    pk, msg, sig = (bytes.fromhex(RFC[0][0]), b"", bytes.fromhex(RFC[0][2]))
    order = 2**252 + 27742317777372353535851937790883648493
    s = int.from_bytes(sig[32:], "little") + order  # same point, non-canonical scalar
    assert s < 2**256
    assert not verifier.ed25519_verify(pk, msg, sig[:32] + s.to_bytes(32, "little"))
    assert not verifier.ed25519_verify(pk[:31], msg, sig)
    assert not verifier.ed25519_verify(pk, msg, sig[:63])
    assert not verifier.ed25519_verify(b"\xff" * 32, msg, sig)  # y >= p: not a canonical point


def test_public_key_pem_and_key_id_match_the_main_code() -> None:
    ed = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives import serialization

    from nuramesh.engine.anchors import key_id

    key = ed.Ed25519PrivateKey.from_private_bytes(bytes([5]) * 32).public_key()
    pem = key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert verifier.load_public_key(pem.decode()) == raw
    assert verifier.key_id(raw) == key_id(key)
    with pytest.raises(verifier.KeyError_):
        verifier.load_public_key("-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n")
