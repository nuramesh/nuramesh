"""Strict Ed25519 pre-checks (spec SV-14; Fable fix F-17): pure stdlib arithmetic on the encodings
of a public key and a signature, so "strict" is a tested property and not a claim: a non-canonical
point encoding (y >= p), a point off the curve, a small-order A or R (the cofactor-8 subgroup) and
a non-reduced S (>= L) are each refused with a code before any library sees them. The engine's
`verify_strict` runs these first and the library's verification second. Keys in these tests are
generated when the test runs; the small-order encodings are curve constants, not keys."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("cryptography")  # the core-only CI step has no extras; the full tier does

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from nuramesh.core.vm_ed25519 import (
    D,
    L,
    P,
    decode_point,
    is_small_order,
    signature_problem,
    small_order_encodings,
)

ORDER_EIGHT_Y = (
    "26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05",
    "c7176a703d4dd84fba3c0b760d10670f2a2053fa2c39ccc64ec7fd7792ac037a",
)
"""The two y-encodings of the order-8 points (libsodium's blocklist); the other small-order
points have y in {0, 1, p - 1}."""


def fresh() -> tuple[bytes, Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return public, private


def test_curve_constants() -> None:
    assert P == 2**255 - 19
    assert L == 2**252 + 27742317777372353535851937790883648493
    assert D == (-121665 * pow(121666, -1, P)) % P


def test_decode_point_accepts_a_fresh_public_key_and_refuses_bad_encodings() -> None:
    public, _ = fresh()
    point = decode_point(public)
    assert point is not None
    x, y = point
    assert (-x * x + y * y - 1 - D * x * x * y * y) % P == 0  # on the curve
    assert decode_point(b"\x00" * 31) is None  # length
    non_canonical = (P + 1).to_bytes(32, "little")  # y = p + 1 encodes the same y as 1
    assert decode_point(non_canonical) is None
    assert (
        decode_point(bytes([0xFF] * 32)) is None
    )  # y = 2^255 - 1 with the sign bit: not canonical
    assert decode_point((2).to_bytes(32, "little")) is None  # y = 2 has no x on the curve
    # x = 0 with the sign bit set is not a valid encoding
    assert decode_point((1 | (1 << 255)).to_bytes(32, "little")) is None


def test_small_order_points_are_exactly_the_cofactor_subgroup() -> None:
    encodings = small_order_encodings()
    assert len(encodings) == 8
    ys = sorted({int.from_bytes(e, "little") & ((1 << 255) - 1) for e in encodings})
    order_eight = [int.from_bytes(bytes.fromhex(y), "little") for y in ORDER_EIGHT_Y]
    assert ys == sorted([0, 1, P - 1, *order_eight])
    for enc in encodings:
        point = decode_point(enc)
        assert point is not None and is_small_order(point)
    public, _ = fresh()
    point = decode_point(public)
    assert point is not None and not is_small_order(point)
    identity = (1).to_bytes(32, "little")
    assert decode_point(identity) == (0, 1) and is_small_order((0, 1))


def test_signature_problem_codes() -> None:
    public, private = fresh()
    message = b"nuramesh-vm-verdict-v1\n" + b"{}"
    sig = private.sign(message)
    assert signature_problem(public, sig) is None
    assert signature_problem(public[:-1], sig) == "public_key_length"
    assert signature_problem(public, sig[:-1]) == "signature_length"
    assert signature_problem((P + 1).to_bytes(32, "little"), sig) == "public_key_not_canonical"
    assert signature_problem((2).to_bytes(32, "little"), sig) == "public_key_not_on_curve"
    for enc in small_order_encodings():
        assert signature_problem(enc, sig) == "public_key_small_order"
        assert signature_problem(public, enc + sig[32:]) == "r_small_order"
    assert signature_problem(public, (P + 1).to_bytes(32, "little") + sig[32:]) == "r_not_canonical"
    assert signature_problem(public, (2).to_bytes(32, "little") + sig[32:]) == "r_not_on_curve"
    s = int.from_bytes(sig[32:], "little")
    bumped = sig[:32] + (s + L).to_bytes(32, "little")
    assert signature_problem(public, bumped) == "s_not_reduced"
    assert signature_problem(public, sig[:32] + L.to_bytes(32, "little")) == "s_not_reduced"
    assert (
        signature_problem(public, sig[:32] + bytes(32)) is None
    )  # S = 0 is reduced (the library decides)


@pytest.mark.parametrize("round_", [1, 2, 3])
def test_random_keys_pass_the_pre_checks(round_: int) -> None:
    seed = os.urandom(32)  # made here, not at collection: xdist workers must collect equal ids
    private = Ed25519PrivateKey.from_private_bytes(seed)
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert signature_problem(public, private.sign(seed)) is None
