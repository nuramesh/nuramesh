"""R1.38 MFA, pure TOTP and recovery-code functions (spec/identity/mfa.md section 1, T1-T8)."""

from __future__ import annotations

import hashlib
import hmac as hmac_module
import re
from typing import Any

import pytest

from nuramesh.core import identity

SECRET = b"12345678901234567890"
B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_t1_rfc6238_vectors() -> None:
    cases = [
        (59, "287082", 1),
        (1111111109, "081804", 37037036),
        (1111111111, "050471", 37037037),
        (1234567890, "005924", 41152263),
        (2000000000, "279037", 66666666),
        (20000000000, "353130", 666666666),
    ]
    for now, code, step in cases:
        assert identity.verify_totp(SECRET, code, now, None) == step


def test_t2_window() -> None:
    assert identity.verify_totp(SECRET, "755224", 59, None) == 0
    assert identity.verify_totp(SECRET, "359152", 59, None) == 2
    assert identity.verify_totp(SECRET, "969429", 59, None) is None


def test_t3_replay() -> None:
    assert identity.verify_totp(SECRET, "287082", 59, None) == 1
    assert identity.verify_totp(SECRET, "287082", 59, 1) is None
    assert identity.verify_totp(SECRET, "287082", 70, 1) is None
    assert identity.verify_totp(SECRET, "359152", 70, 1) == 2


@pytest.mark.parametrize("code", ["28708", "2870820", "287 082", "٢٨٧٠٨٢", "+287082", ""])
def test_t4_format_no_hmac(code: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = hmac_module.new

    def spy(*a: Any, **k: Any) -> Any:
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(hmac_module, "new", spy)
    assert identity.verify_totp(SECRET, code, 59, None) is None
    assert calls == []


@pytest.mark.parametrize("code", ["287082", "000000"])
def test_t5_constant_work(code: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    real = hmac_module.new

    def spy(*a: Any, **k: Any) -> Any:
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(hmac_module, "new", spy)
    identity.verify_totp(SECRET, code, 59, None)
    assert len(calls) == 3


def test_t6_new_secret() -> None:
    a, b = identity.new_totp_secret(), identity.new_totp_secret()
    assert len(a) == 20 and a != b
    text = identity.secret_to_base32(a)
    assert re.fullmatch(r"[A-Z2-7]{32}", text)
    assert identity.secret_from_base32(text) == a
    assert identity.secret_to_base32(SECRET) == B32


def test_t7_uri() -> None:
    assert identity.provisioning_uri("NuraMesh", "ada@example.org", SECRET) == (
        "otpauth://totp/NuraMesh:ada%40example.org?secret="
        + B32
        + "&issuer=NuraMesh&algorithm=SHA1&digits=6&period=30"
    )


def test_t8_recovery_codes() -> None:
    codes = identity.new_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    for c in codes:
        assert re.fullmatch(r"[A-HJ-NP-Z2-9]{4}(-[A-HJ-NP-Z2-9]{4}){3}", c)
    assert identity.normalise_recovery_code("abcd efgh-jklm-npqr") == "ABCDEFGHJKLMNPQR"
    for bad in [
        "ABCDEFGHIJKLMNPQ",
        "ABCD-EFGH-JKLM-NPQ0",
        "ABCD-EFGH-JKLM-NPQ1",
        "ABCDEFGHJKLMNPQ",
    ]:
        assert identity.normalise_recovery_code(bad) is None
    n = identity.normalise_recovery_code(codes[0])
    assert n is not None
    assert identity.recovery_code_hash(n) == hashlib.sha256(n.encode()).hexdigest()


def test_normalising_a_recovery_code_upper_cases_ascii_only() -> None:
    """R1.38 m5: "\u00df".upper() is "SS"; it must not make 15 symbols into a valid 16."""
    assert identity.normalise_recovery_code("\u00df" + "ABCDEFGHJKLMNP") is None
    assert identity.normalise_recovery_code("abcdefghjklmnpqr") == "ABCDEFGHJKLMNPQR"
