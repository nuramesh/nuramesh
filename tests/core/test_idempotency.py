"""Idempotency rules: header (H), fingerprint (F), outcome function (D), envelope (spec 15)."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from nuramesh.core.idempotency import (
    IDEMPOTENCY_TTL,
    Delivery,
    InvalidKey,
    Mode,
    RouteMode,
    Row,
    Scope,
    Sealed,
    State,
    associated_data,
    decide,
    expiry,
    fingerprint,
    honored,
    key_hash,
    lease_end,
    pack_response,
    parse_key,
    storable,
    unpack_response,
)

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def fp(
    body: bytes = b"",
    *,
    path: str = "/v1/runs/r1/stop",
    query: Sequence[tuple[str, str]] = (),
    ct: str | None = None,
) -> str:
    return fingerprint("POST", path, query, ct, body)


@pytest.mark.parametrize(
    "value",
    [
        "abcdefghijklmnop",
        "a" * 128,
        "7f1d7c0e-9b1e-4a4e-8d54-2a8f0c1d5e61",
        "a.b_c-d.e_f-g.h_i-j",
        "  abcdefghijklmnop  ",
    ],
)
def test_accepted_keys(value: str) -> None:
    assert parse_key([value]) == value.strip()


@pytest.mark.parametrize(
    "values",
    [
        ["a" * 15],
        ["a" * 129],
        [""],
        ["abcdefgh ijklmnop"],
        ["abcdefgh/ijklmnop"],
        ["abcdefghéijklmno"],
        ["abcdefghijklmnop", "abcdefghijklmnop"],
        ["abcdefgh,ijklmnop"],
        [],
    ],
)
def test_refused_keys(values: list[str]) -> None:
    with pytest.raises(InvalidKey) as err:
        parse_key(values)
    assert not any(v and v in str(err.value) for v in values)


def test_h13_case_sensitive() -> None:
    assert key_hash("Abcdefghijklmnop") != key_hash("abcdefghijklmnop")


def test_key_hash_is_sha256_hex() -> None:
    import hashlib

    got = key_hash("abcdefghijklmnop")
    assert got == hashlib.sha256(b"abcdefghijklmnop").hexdigest()
    assert len(got) == 64


def test_f01_to_f03() -> None:
    assert fp(b'{"a":1,"b":2}') == fp(b'{"b":2,"a":1}')
    assert fp(b'{"a":1}') == fp(b'{ "a" : 1 }')
    assert fp(b'{"a":1}') != fp(b'{"a":2}')


def test_f04_no_normalization() -> None:
    pre = '{"r":"é"}'.encode()
    dec = '{"r":"é"}'.encode()
    assert fp(pre) != fp(dec)


def test_f05_empty_vs_object() -> None:
    assert fp(b"") != fp(b"{}")


def test_f06_f07_query() -> None:
    assert fp(query=[("a", "1"), ("b", "2")]) == fp(query=[("b", "2"), ("a", "1")])
    assert fp(query=[("a", "1")]) != fp(query=[("a", "2")])


def test_f08_path() -> None:
    assert fp(path="/v1/runs/r1/stop") != fp(path="/v1/runs/r2/stop")


def test_f10_charset_param() -> None:
    body = b'{"a":1}'
    assert fp(body, ct="application/json; charset=utf-8") == fp(body, ct="application/json")


def test_f11_f12_raw() -> None:
    assert fp(b'{"a":1.0}') != fp(b'{"a":1}')
    assert fp(b'{"a":9007199254740993}') != fp(b'{"a":9007199254740992}')
    assert fp(b'{"a":9007199254740991}') == fp(b'{ "a": 9007199254740991 }')


def test_f13_repeated_key_raw() -> None:
    assert fp(b'{"a":1,"a":2}') != fp(b'{"a":2}')


def test_f14_not_utf8() -> None:
    assert fp(b"\xff\xfe") != fp(b"\xff\xfd")


def test_f15_f16() -> None:
    assert fp(b'{"a":[1,2]}') != fp(b'{"a":[2,1]}')
    assert fp(b'{"a":null}') != fp(b"{}")


def test_f19_escape_equals_bytes() -> None:
    assert fp(b'{"r":"\\u00e9"}') == fp('{"r":"é"}'.encode())


def test_f20_empty_body_ignores_content_type() -> None:
    a = fp(b"")
    assert a == fp(b"", ct="application/json") == fp(b"", ct="application/json; charset=utf-8")
    assert a != fp(b"{}", ct="application/json")


def test_lone_surrogate_is_raw() -> None:
    assert len(fp(b'{"a":"\\ud800"}')) == 64


def _row(
    state: State = State.IN_PROGRESS, status: int | None = None, sealed: Sealed | None = None
) -> Row:
    return Row(
        state=state,
        fingerprint="a" * 64,
        attempt=1,
        lease_until=T0 + timedelta(seconds=120),
        expires_at=T0 + timedelta(seconds=IDEMPOTENCY_TTL),
        status=status,
        sealed=sealed,
    )


def test_decide_table() -> None:
    A, B = "a" * 64, "b" * 64
    s5 = T0 + timedelta(seconds=5)
    assert decide(None, A, T0) == "insert"
    assert decide(_row(), A, s5) == "in_progress"  # D04
    assert decide(_row(), B, s5) == "mismatch"  # D05
    s121 = T0 + timedelta(seconds=121)
    assert decide(_row(), A, s121) == "takeover"  # D06
    assert decide(_row(), B, s121) == "mismatch"  # D07
    sealed = Sealed(b"x" * 20, b"n" * 12, "0" * 16)
    done = _row(State.DONE, 200, sealed)
    assert decide(done, A, s5) == "replay"  # D02
    assert decide(done, B, s5) == "mismatch"  # D03


def test_decide_expiry_boundary() -> None:
    done = _row(State.DONE, 200)
    end = T0 + timedelta(seconds=IDEMPOTENCY_TTL)
    assert decide(done, "a" * 64, end - timedelta(seconds=1)) == "replay"  # D10
    assert decide(done, "a" * 64, end) == "insert"  # D09
    assert decide(done, "b" * 64, end) == "insert"


def test_ttl_is_24_hours_and_lease_120() -> None:
    assert IDEMPOTENCY_TTL == 86_400
    assert lease_end(T0) - T0 == timedelta(seconds=120)
    assert expiry(T0) - T0 == timedelta(hours=24)


def test_envelope_roundtrip_and_allowlist() -> None:
    headers = {
        "Content-Type": "application/json",
        "Set-Cookie": "x=y",
        "Date": "now",
        "Location": "/v1/x",
        "Cache-Control": "no-store",
    }
    data = pack_response(headers, b'{"ok":true}')
    got, body = unpack_response(data)
    assert body == b'{"ok":true}'
    assert got == {
        "content-type": "application/json",
        "location": "/v1/x",
        "cache-control": "no-store",
    }
    assert b"Set-Cookie" not in data and b"set-cookie" not in data


def test_unpack_rejects_garbage() -> None:
    for bad in (b"", b"\x00\x00\x00\xff{}", b"\x00\x00\x00\x02[]"):
        with pytest.raises(ValueError):
            unpack_response(bad)


def test_storable_limits() -> None:
    assert storable(200, {}, b"x" * 262_144)  # D19
    assert not storable(200, {}, b"x" * 262_145)  # D20
    assert not storable(200, {"Set-Cookie": "a=b"}, b"")  # D21
    assert storable(201, {}, b"")  # D12
    for status in (199, 300, 404, 409, 422, 500, 503):
        assert not storable(status, {}, b"")


def test_associated_data_binds_every_part() -> None:
    s = Scope("acme", "human:u_1111", "POST", "/v1/runs/{run_id}/stop")
    k, f = "k" * 64, "f" * 64
    base = associated_data(s, k, f, 200)
    others = [
        associated_data(Scope("beta", s.actor, s.method, s.route), k, f, 200),
        associated_data(Scope(s.org_id, "human:u_2222", s.method, s.route), k, f, 200),
        associated_data(Scope(s.org_id, s.actor, "PUT", s.route), k, f, 200),
        associated_data(Scope(s.org_id, s.actor, s.method, "/other"), k, f, 200),
        associated_data(s, "j" * 64, f, 200),
        associated_data(s, k, "e" * 64, 200),
        associated_data(s, k, f, 201),
    ]
    assert all(o != base for o in others)
    # length framing: moving a character between fields changes the bytes
    a = associated_data(Scope("ab", "c", "POST", "r"), k, f, 200)
    b = associated_data(Scope("a", "bc", "POST", "r"), k, f, 200)
    assert a != b


def test_route_mode_invariant() -> None:
    assert honored(Delivery.EXACTLY_ONCE).mode is Mode.HONORED
    with pytest.raises(ValueError):
        RouteMode(Mode.EXCLUDED_SECRET, Delivery.EXACTLY_ONCE)
    with pytest.raises(ValueError):
        RouteMode(Mode.HONORED)
