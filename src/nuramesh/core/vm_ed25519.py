"""Strict Ed25519 pre-checks (spec `spec/verified-merge` SV-14; Fable fix F-17).

RFC 8032 leaves two things to the verifier that "strict" verification pins down: it must refuse a
signature whose `S` is not reduced (`S >= L`, else the signature is malleable) and it must refuse a
public key `A` or a nonce point `R` that is not a canonical encoding or that lies in the small
(cofactor 8) subgroup, where a signature can be valid for a message it was never made for. The
`cryptography` library refuses `S >= L` and off-curve points but not small-order `A` or `R`, and it
does not expose which check failed. This module is the open core's statement of what "strict"
refuses: stdlib integer arithmetic on the encodings, returning a code, before any library sees
the bytes. It never verifies a signature on its own: `engine/vm_keys.verify_strict` runs these
checks first and the library second. The affine group law is used only to multiply by the
cofactor, so there is no performance-sensitive arithmetic here.
"""

from __future__ import annotations

from collections.abc import Iterable

P = 2**255 - 19
"""The field prime."""
L = 2**252 + 27742317777372353535851937790883648493
"""The order of the prime-order subgroup (RFC 8032 section 5.1)."""
D = (-121665 * pow(121666, -1, P)) % P
"""The curve constant d of -x^2 + y^2 = 1 + d x^2 y^2."""
_SQRT_M1 = pow(2, (P - 1) // 4, P)
"""A square root of -1 (2^((p-1)/4))."""
_SIGN_BIT = 1 << 255
_Y_MASK = _SIGN_BIT - 1

Point = tuple[int, int]
IDENTITY: Point = (0, 1)

PROBLEM_CODES = (
    "public_key_length",
    "signature_length",
    "public_key_not_canonical",
    "public_key_not_on_curve",
    "public_key_small_order",
    "r_not_canonical",
    "r_not_on_curve",
    "r_small_order",
    "s_not_reduced",
)


def decode_point(encoded: bytes) -> Point | None:
    """The affine point of a 32-byte encoding, or None when the encoding is not 32 bytes, is not
    canonical (y >= p), has no x on the curve, or sets the sign bit with x = 0 (RFC 8032 5.1.3,
    with the canonical-encoding rule added)."""
    if len(encoded) != 32:
        return None
    value = int.from_bytes(encoded, "little")
    sign = value >> 255
    y = value & _Y_MASK
    if y >= P:
        return None
    y2 = y * y % P
    u = (y2 - 1) % P
    v = (D * y2 + 1) % P
    x2 = u * pow(v, -1, P) % P
    if x2 == 0:
        if sign:
            return None
        return (0, y)
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P != 0:
        x = x * _SQRT_M1 % P
        if (x * x - x2) % P != 0:
            return None
    if (x & 1) != sign:
        x = P - x
    return (x, y)


def encode_point(point: Point) -> bytes:
    x, y = point
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def on_curve(point: Point) -> bool:
    x, y = point
    return (-x * x + y * y - 1 - D * x * x * y * y) % P == 0


def add(p1: Point, p2: Point) -> Point:
    """The complete affine addition law of the twisted Edwards curve (a = -1)."""
    x1, y1 = p1
    x2, y2 = p2
    dxy = D * x1 * x2 % P * y1 % P * y2 % P
    x3 = (x1 * y2 + x2 * y1) * pow(1 + dxy, -1, P) % P
    y3 = (y1 * y2 + x1 * x2) * pow(1 - dxy, -1, P) % P
    return (x3, y3)


def scalar_mult(k: int, point: Point) -> Point:
    result = IDENTITY
    base = point
    while k > 0:
        if k & 1:
            result = add(result, base)
        base = add(base, base)
        k >>= 1
    return result


def is_small_order(point: Point) -> bool:
    """True when 8 * point is the identity: the point is in the cofactor subgroup (the identity
    included), so it never belongs to a key or nonce made by RFC 8032."""
    return scalar_mult(8, point) == IDENTITY


def small_order_encodings() -> tuple[bytes, ...]:
    """The canonical encodings of the eight small-order points, computed from the curve: y in
    {0, 1, p - 1} (x = 0 or x = +/- sqrt(-1)) and the two y-values of the order-8 points (the
    roots of d y^4 + 2 y^2 - 1 = 0), each with both signs of x. Sorted bytes."""
    found: set[bytes] = set()
    for y in _small_order_ys():
        for sign in (0, 1):
            encoded = (y | (sign << 255)).to_bytes(32, "little")
            point = decode_point(encoded)
            if point is not None and is_small_order(point):
                found.add(encoded)
    return tuple(sorted(found))


def _sqrt(value: int) -> int | None:
    root = pow(value, (P + 3) // 8, P)
    if (root * root - value) % P == 0:
        return root
    root = root * _SQRT_M1 % P
    if (root * root - value) % P == 0:
        return root
    return None


def _small_order_ys() -> Iterable[int]:
    yield 0
    yield 1
    yield P - 1
    # order-8 points: 2P has y = 0, so y^2 + x^2 = 0 on the curve, d y^4 + 2 y^2 - 1 = 0
    disc = _sqrt((1 + D) % P)
    if disc is None:  # pragma: no cover - a property of the curve
        return
    for root in (disc, P - disc):
        y2 = (root - 1) * pow(D, -1, P) % P
        y = _sqrt(y2)
        if y is not None:
            yield y
            yield P - y


def signature_problem(public_key: bytes, signature: bytes) -> str | None:
    """Why strict verification refuses these bytes without looking at the message, as one of
    `PROBLEM_CODES`, or None when the library may now verify (which can still fail)."""
    if len(public_key) != 32:
        return "public_key_length"
    if len(signature) != 64:
        return "signature_length"
    a_value = int.from_bytes(public_key, "little") & _Y_MASK
    if a_value >= P:
        return "public_key_not_canonical"
    a_point = decode_point(public_key)
    if a_point is None:
        return "public_key_not_on_curve"
    if is_small_order(a_point):
        return "public_key_small_order"
    r_bytes = signature[:32]
    r_value = int.from_bytes(r_bytes, "little") & _Y_MASK
    if r_value >= P:
        return "r_not_canonical"
    r_point = decode_point(r_bytes)
    if r_point is None:
        return "r_not_on_curve"
    if is_small_order(r_point):
        return "r_small_order"
    if int.from_bytes(signature[32:], "little") >= L:
        return "s_not_reduced"
    return None
