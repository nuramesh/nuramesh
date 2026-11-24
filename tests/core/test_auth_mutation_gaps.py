"""Behaviour pins for `nuramesh.core.auth` found by mutation testing (limits, formats, exact
values, frozen models, boundaries). Secrets here are generated when the test runs."""

import base64
import dataclasses
import hashlib
import hmac
import ipaddress
import json
import secrets
import unicodedata
from collections import deque
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core import auth
from nuramesh.core.auth import (
    AgentCredential,
    ApiToken,
    AuthFailure,
    AuthSession,
    AuthToken,
    CallCap,
    Credential,
    LockoutPolicy,
    Permission,
    Role,
    ScryptParams,
    SecurityLogEntry,
    SetupToken,
    TokenScope,
    User,
)

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
UID = "human:u_" + "ab12" * 5
HEX64 = "ab" * 32
SMALL = ScryptParams(n=2, r=1, p=1, salt_bytes=8, key_bytes=16)


def _pw() -> str:
    return "pw-" + secrets.token_urlsafe(18)


class _NoOffset(tzinfo):
    """A tzinfo that is set but knows no offset: still a naive time."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


# ---- emails, times, users --------------------------------------------------------------------


def test_email_length_limit_is_254() -> None:
    assert auth.MAX_EMAIL_LENGTH == 254
    ok = "a" * (254 - len("@b.co")) + "@b.co"
    assert auth.normalize_email(ok) == ok
    with pytest.raises(ValueError):
        auth.normalize_email("a" + ok)


def test_aware_checks_zone_and_passes_none() -> None:
    assert auth._aware(None) is None
    assert auth._aware(NOW) == NOW
    with pytest.raises(ValueError):
        auth._aware(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        auth._aware(datetime(2026, 1, 1, tzinfo=_NoOffset()))


def _user(**kw: Any) -> User:
    base: dict[str, Any] = {
        "org_id": "org",
        "id": UID,
        "email": "a@b.co",
        "role": Role.VIEWER,
        "created_at": NOW,
    }
    base.update(kw)
    return User(**base)


def test_user_erasure_consistency_rules() -> None:
    assert _user().erased is False
    with pytest.raises(ValidationError):
        _user(email=None)  # not erased: needs an email
    with pytest.raises(ValidationError):
        _user(email=None, display_name="Name")  # not erased, still no email
    gone = _user(email=None, display_name=None, erased_at=NOW)
    assert gone.erased is True
    with pytest.raises(ValidationError):
        _user(email=None, display_name="Name", erased_at=NOW)  # name left behind
    with pytest.raises(ValidationError):
        _user(email="a@b.co", display_name=None, erased_at=NOW)  # email left behind
    assert _user(display_name="Name").display_name == "Name"


def test_user_disabled_flag_and_limits_and_frozen() -> None:
    assert _user().disabled is False
    assert _user(disabled_at=NOW).disabled is True
    assert _user(org_id="x").org_id == "x"
    assert _user(org_id="x" * 128).org_id == "x" * 128
    for bad in ("", "x" * 129):
        with pytest.raises(ValidationError):
            _user(org_id=bad)
    assert _user(display_name="n" * 200).display_name == "n" * 200
    with pytest.raises(ValidationError):
        _user(display_name="n" * 201)
    with pytest.raises(ValidationError):
        _user().role = Role.OWNER
    with pytest.raises(ValidationError) as err:
        _user(id="not-a-person-marker-zzz")
    assert "not-a-person-marker-zzz" not in str(err.value)  # hide_input_in_errors
    assert "a@b.co" not in repr(_user()) and "Name" not in repr(_user(display_name="Name"))


# ---- scrypt parameters ------------------------------------------------------------------------


def test_scrypt_default_values_and_maxmem() -> None:
    d = auth.DEFAULT_PARAMS
    assert (d.n, d.r, d.p, d.salt_bytes, d.key_bytes) == (1 << 15, 8, 3, 16, 32)
    assert d.memory_bytes == 128 * 8 * ((1 << 15) + 3 + 2)
    assert d.maxmem == d.memory_bytes + (1 << 20)
    assert SMALL.maxmem == 128 * (2 + 1 + 2) + 1048576


@pytest.mark.parametrize("n", [2, 4, 8, 1024, 1 << 15, 1 << 20])
def test_scrypt_n_powers_of_two_accepted(n: int) -> None:
    assert ScryptParams(n=n, r=1, p=1).n == n


@pytest.mark.parametrize("n", [-2, 0, 1, 3, 6, 12, (1 << 15) + 1, 1 << 21, 1 << 22])
def test_scrypt_n_rejected(n: int) -> None:
    with pytest.raises(ValidationError):
        ScryptParams(n=n, r=1, p=1)


def test_scrypt_r_p_bounds() -> None:
    assert ScryptParams(n=2, r=32, p=16).r == 32
    assert ScryptParams(n=2, r=1, p=1).p == 1
    for r, p in [(0, 1), (33, 1), (1, 0), (1, 17)]:
        with pytest.raises(ValidationError):
            ScryptParams(n=2, r=r, p=p)


def test_scrypt_salt_and_key_bounds() -> None:
    assert ScryptParams(n=2, r=1, p=1, salt_bytes=8).salt_bytes == 8
    assert ScryptParams(n=2, r=1, p=1, salt_bytes=64).salt_bytes == 64
    assert ScryptParams(n=2, r=1, p=1, key_bytes=16).key_bytes == 16
    assert ScryptParams(n=2, r=1, p=1, key_bytes=64).key_bytes == 64
    for kw in ({"salt_bytes": 7}, {"salt_bytes": 65}, {"key_bytes": 15}, {"key_bytes": 65}):
        with pytest.raises(ValidationError):
            ScryptParams(n=2, r=1, p=1, **kw)


def test_scrypt_memory_cap_is_one_gib() -> None:
    assert ScryptParams(n=1 << 19, r=8, p=1).memory_bytes <= 1 << 30
    with pytest.raises(ValidationError):
        ScryptParams(n=1 << 20, r=8, p=1)  # 1 GiB + a few KiB
    assert ScryptParams(n=1 << 20, r=1, p=1).memory_bytes < 1 << 30


def test_scrypt_params_is_a_frozen_model_that_hides_input_in_errors() -> None:
    params = ScryptParams(n=2, r=1, p=1)
    assert isinstance(params, ScryptParams)
    with pytest.raises(ValidationError):
        params.n = 4
    with pytest.raises(ValidationError) as err:
        ScryptParams(n=2, r=1, p=1, bogus="marker-not-a-secret-zzz")  # type: ignore[call-arg]
    assert "marker-not-a-secret-zzz" not in str(err.value)


# ---- hash format --------------------------------------------------------------------------------


def test_unb64_edge_cases() -> None:
    assert auth._unb64("") is None
    assert auth._unb64("ab+/") is None  # not the url-safe alphabet
    assert auth._unb64("ab=") is None
    for size in range(1, 12):
        data = secrets.token_bytes(size)
        assert auth._unb64(auth._b64(data)) == data


def test_hash_password_format_is_exact() -> None:
    pw = _pw()
    stored = auth.hash_password(pw, SMALL)
    alg, n, r, p, salt, key = stored.split("$")
    assert (alg, n, r, p) == ("scrypt", "2", "1", "1")
    salt_b, key_b = auth._unb64(salt), auth._unb64(key)
    assert salt_b is not None and len(salt_b) == 8
    assert key_b is not None and len(key_b) == 16
    assert key_b == hashlib.scrypt(
        pw.encode(), salt=salt_b, n=2, r=1, p=1, maxmem=SMALL.maxmem, dklen=16
    )


def test_parse_hash_values_and_repr_hides_key() -> None:
    stored = auth.hash_password(_pw(), SMALL)
    parsed = auth.parse_hash(stored)
    assert parsed is not None
    assert parsed.params == SMALL
    assert len(parsed.salt) == 8 and len(parsed.key) == 16
    assert repr(parsed.key) not in repr(parsed) and auth._b64(parsed.key) not in repr(parsed)
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.salt = b"x"  # type: ignore[misc]


def test_parse_hash_refuses_each_malformed_field() -> None:
    salt, key = auth._b64(b"s" * 8), auth._b64(b"k" * 16)
    good = f"scrypt$2$1$1${salt}${key}"
    assert auth.parse_hash(good) is not None
    for bad in (
        f"argon$2$1$1${salt}${key}",
        f"scrypt$2$1${salt}${key}",  # five parts
        f"scrypt$2$1$1${salt}${key}$x",  # seven parts
        f"scrypt$02$1$1${salt}${key}",  # leading zero
        f"scrypt$2$0$1${salt}${key}",
        f"scrypt$2$1$x${salt}${key}",
        f"scrypt$2$1$12345678901${salt}${key}",  # eleven digits
        f"scrypt$2$1$1$${key}",  # empty salt
        f"scrypt$2$1$1${salt}$",  # empty key
        f"scrypt$2$1$1$!!!${key}",  # salt not base64
        f"scrypt$2$1$1${salt}$!!!",  # key not base64
        f"scrypt$3$1$1${salt}${key}",  # n not a power of two
        f"scrypt$2$1$1${auth._b64(b's' * 7)}${key}",  # salt too short
    ):
        assert auth.parse_hash(bad) is None, bad


def test_parse_hash_ten_digit_numbers_are_read_but_refused_by_bounds() -> None:
    salt, key = auth._b64(b"s" * 8), auth._b64(b"k" * 16)
    assert auth.parse_hash(f"scrypt$2$1$9999999999${salt}${key}") is None


def test_parse_hash_with_bad_salt_and_good_key_is_refused() -> None:
    key = auth._b64(b"k" * 16)
    assert auth.parse_hash(f"scrypt$2$1$1$!${key}") is None
    assert auth.parse_hash(f"scrypt$2$1$1${auth._b64(b'a' * 8)}$!") is None


# ---- verify -------------------------------------------------------------------------------------


def test_verify_password_roundtrip_and_wrong() -> None:
    pw = _pw()
    stored = auth.hash_password(pw, SMALL)
    assert auth.verify_password(pw, stored) is True
    assert auth.verify_password(pw + "x", stored) is False
    assert auth.verify_password(pw, "scrypt$bad") is False


def test_verify_password_long_input_is_refused_before_normalising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = auth.hash_password(_pw(), SMALL)
    calls: list[int] = []
    real = unicodedata.normalize

    def spy(form: Any, text: str) -> str:
        calls.append(len(text))
        return str(real(form, text))

    monkeypatch.setattr(unicodedata, "normalize", spy)
    too_long = "a" * (auth.MAX_PASSWORD_LENGTH * 4 + 1)
    assert auth.verify_password(too_long, stored) is False
    assert calls == []  # the cheap bound answered, no NFKC of the long text
    assert auth.verify_password("a" * (auth.MAX_PASSWORD_LENGTH * 4), stored) is False
    assert calls != []  # at exactly 4x the long text is normalised, then refused on length


def test_verify_password_length_limit_after_normalisation() -> None:
    ok = "p" * auth.MAX_PASSWORD_LENGTH
    stored = auth.hash_password(ok, SMALL)
    assert auth.verify_password(ok, stored) is True
    assert auth.verify_password(ok + "p", stored) is False
    assert auth.verify_password("\ud800" * 14, stored) is False  # lone surrogate: no raise


def test_password_length_rules() -> None:
    assert auth.MIN_PASSWORD_LENGTH == 12 and auth.MAX_PASSWORD_LENGTH == 1024
    assert auth.password_problem("") == "the password is empty"
    assert auth.password_problem("x" * 11) is not None
    assert auth.password_problem("x" * 12) is None
    assert auth.password_problem("x" * 1024) is None
    assert auth.password_problem("x" * 1025) is not None
    assert auth.password_problem("é" * 600) is None  # 1200 code points, 600 after NFKC


def test_hash_password_refuses_short_passwords() -> None:
    with pytest.raises(auth.PasswordRuleError):
        auth.hash_password("short", SMALL)


def test_verify_unknown_is_false_and_does_work(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = auth.verify_password

    def spy(password: str, stored: str) -> bool:
        seen.append(stored)
        return real(password, stored)

    monkeypatch.setattr(auth, "verify_password", spy)
    assert auth.verify_unknown(_pw(), SMALL) is False
    assert len(seen) == 1
    parsed = auth.parse_hash(seen[0])
    assert parsed is not None and parsed.params == SMALL


def test_needs_rehash_semantics() -> None:
    stored = auth.hash_password(_pw(), SMALL)
    assert auth.needs_rehash(stored, SMALL) is False
    assert auth.needs_rehash(stored, ScryptParams(n=4, r=1, p=1, salt_bytes=8, key_bytes=16))
    assert auth.needs_rehash("garbage", SMALL) is True


# ---- credential ---------------------------------------------------------------------------------


def test_credential_keeps_values_and_hides_hash() -> None:
    stored = auth.hash_password(_pw(), SMALL)
    cred = Credential(org_id="o", user_id=UID, password_hash=stored, updated_at=NOW)
    assert cred.user_id == UID
    assert cred.password_hash == stored
    assert cred.updated_at == NOW
    assert stored not in repr(cred)
    with pytest.raises(ValidationError):
        cred.user_id = UID
    with pytest.raises(ValidationError) as err:
        Credential(org_id="o", user_id=UID, password_hash="hash-marker-zzz", updated_at=NOW)
    assert "hash-marker-zzz" not in str(err.value)
    with pytest.raises(ValidationError):
        Credential(org_id="o", user_id=UID, password_hash=stored, updated_at=datetime(2026, 1, 1))
    for bad in ("", "x" * 129):
        with pytest.raises(ValidationError):
            Credential(org_id=bad, user_id=UID, password_hash=stored, updated_at=NOW)
    for good in ("x", "x" * 128):
        Credential(org_id=good, user_id=UID, password_hash=stored, updated_at=NOW)


# ---- setup token --------------------------------------------------------------------------------


def test_setup_token_model_is_frozen_hidden_and_bounded() -> None:
    rec = SetupToken(org_id="o", token_hash=HEX64, created_at=NOW, expires_at=NOW)
    with pytest.raises(ValidationError):
        rec.org_id = "p"
    with pytest.raises(ValidationError) as err:
        SetupToken(org_id="o", token_hash="hash-marker-zzz", created_at=NOW, expires_at=NOW)
    assert "hash-marker-zzz" not in str(err.value)
    for bad in ("", "x" * 129):
        with pytest.raises(ValidationError):
            SetupToken(org_id=bad, token_hash=HEX64, created_at=NOW, expires_at=NOW)
    for good in ("x", "x" * 128):
        SetupToken(org_id=good, token_hash=HEX64, created_at=NOW, expires_at=NOW)


def test_issued_dataclasses_are_frozen_and_hide_secrets() -> None:
    issued = auth.issue_setup_token("o", NOW)
    assert issued.token not in repr(issued)
    with pytest.raises(dataclasses.FrozenInstanceError):
        issued.token = "x"  # type: ignore[misc]
    api = auth.issue_api_token("o", "name1", {TokenScope.READ}, created_by="service:cli", now=NOW)
    assert api.token not in repr(api)
    with pytest.raises(dataclasses.FrozenInstanceError):
        api.token = "x"  # type: ignore[misc]
    agent = _issue_agent()
    assert agent.credential not in repr(agent)
    with pytest.raises(dataclasses.FrozenInstanceError):
        agent.credential = "x"  # type: ignore[misc]
    sess = auth.issue_tokens(_session(), NOW)
    assert sess.access_token not in repr(sess) and sess.refresh_token not in repr(sess)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sess.access_token = "x"  # type: ignore[misc]


# ---- sessions and tokens ------------------------------------------------------------------------

SID = "0123456789abcdef" * 2


def _session(**kw: Any) -> AuthSession:
    base: dict[str, Any] = {
        "login_via": "password",
        "org_id": "o",
        "id": SID,
        "user_id": UID,
        "created_at": NOW,
        "last_active_at": NOW,
        "absolute_expires_at": NOW + timedelta(days=7),
    }
    base.update(kw)
    return AuthSession(**base)


def test_session_keeps_values_and_is_frozen_and_bounded() -> None:
    s = _session()
    assert s.user_id == UID
    with pytest.raises(ValidationError):
        s.org_id = "p"
    for bad in ("", "x" * 129):
        with pytest.raises(ValidationError):
            _session(org_id=bad)
    for good in ("x", "x" * 128):
        assert _session(org_id=good).org_id == good


def _token(**kw: Any) -> AuthToken:
    base: dict[str, Any] = {
        "org_id": "o",
        "token_hash": HEX64,
        "session_id": SID,
        "kind": auth.TokenKind.ACCESS,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
    }
    base.update(kw)
    return AuthToken(**base)


def test_auth_token_frozen_and_org_bounds() -> None:
    with pytest.raises(ValidationError):
        _token().org_id = "p"
    for bad in ("", "x" * 129):
        with pytest.raises(ValidationError):
            _token(org_id=bad)
    for good in ("x", "x" * 128):
        assert _token(org_id=good).org_id == good


def test_session_constants() -> None:
    assert auth.ACCESS_LIFETIME == timedelta(minutes=15)
    assert auth.REFRESH_IDLE == timedelta(hours=8)
    assert auth.SESSION_ABSOLUTE == timedelta(days=7)
    assert auth.SESSION_RETENTION == timedelta(days=90)
    assert auth.REFRESH_GRACE_MAX == timedelta(seconds=30)
    assert auth.MAX_AUTH_TOKEN_LENGTH == 256
    assert auth.REFRESH_SUCCESSOR_KEY_MIN == 32


def test_successor_tokens_exact_derivation_and_key_minimum() -> None:
    key = secrets.token_bytes(32)
    spent = hashlib.sha256(b"spent").hexdigest()

    def expect(label: bytes) -> str:
        msg = b"\x00".join([label, spent.encode("ascii"), SID.encode()])
        digest = hmac.new(key, msg, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    access, refresh = auth.successor_tokens(key, spent, SID)
    assert access == expect(b"nuramesh-successor-access-v1")
    assert refresh == expect(b"nuramesh-successor-refresh-v1")
    assert len(access) == len(refresh) == 43 and access != refresh
    assert auth.successor_tokens(key + b"x", spent, SID)[0] != access
    with pytest.raises(ValueError):
        auth.successor_tokens(key[:31], spent, SID)
    auth.successor_tokens(key[:32], spent, SID)


def test_issue_tokens_with_successor_uses_key_and_hash_in_order() -> None:
    key = secrets.token_bytes(32)
    spent = hashlib.sha256(b"spent2").hexdigest()
    s = _session()
    issued = auth.issue_tokens(s, NOW, successor=(key, spent))
    access, refresh = auth.successor_tokens(key, spent, s.id)
    assert (issued.access_token, issued.refresh_token) == (access, refresh)
    assert issued.access.token_hash == auth.hash_auth_token(access)
    assert issued.refresh.token_hash == auth.hash_auth_token(refresh)
    assert issued.access.expires_at == NOW + timedelta(minutes=15)
    assert issued.refresh.expires_at == NOW + timedelta(hours=8)


def test_issue_tokens_random_otherwise_and_capped_by_session_end() -> None:
    s = _session(absolute_expires_at=NOW + timedelta(minutes=5))
    a, b = auth.issue_tokens(s, NOW), auth.issue_tokens(s, NOW)
    assert a.access_token != b.access_token and a.refresh_token != b.refresh_token
    assert a.access.expires_at == a.refresh.expires_at == NOW + timedelta(minutes=5)


def test_csrf_token_is_hmac_of_label_keyed_by_refresh_token() -> None:
    refresh = secrets.token_urlsafe(32)
    expected = hmac.new(refresh.encode(), b"nuramesh-csrf-v1", hashlib.sha256).hexdigest()
    assert auth.csrf_token(refresh) == expected
    assert auth.csrf_matches(expected, refresh) is True
    assert (
        auth.csrf_matches(expected[:-1] + ("0" if expected[-1] != "0" else "1"), refresh) is False
    )
    assert auth.csrf_matches(expected, refresh + "x") is False


def test_auth_failure_model() -> None:
    f = AuthFailure(key="k", count=1, last_failure_at=NOW)
    with pytest.raises(ValidationError):
        f.count = 2
    with pytest.raises(ValidationError):
        AuthFailure(key="k", count=0, last_failure_at=NOW)
    with pytest.raises(ValidationError):
        AuthFailure(key="", count=1, last_failure_at=NOW)
    assert AuthFailure(key="k" * 200, count=1, last_failure_at=NOW).key == "k" * 200
    with pytest.raises(ValidationError):
        AuthFailure(key="k" * 201, count=1, last_failure_at=NOW)


def test_lockout_policy_frozen_and_defaults() -> None:
    pol = LockoutPolicy()
    assert (pol.threshold, pol.base, pol.cap, pol.decay) == (
        5,
        timedelta(minutes=1),
        timedelta(minutes=15),
        timedelta(hours=1),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        pol.threshold = 1  # type: ignore[misc]


def test_lockout_doubling_is_capped_at_32_steps() -> None:
    pol = LockoutPolicy(threshold=1, base=timedelta(seconds=1), cap=timedelta(days=10**5))
    assert pol.lock_for(1) == timedelta(seconds=1)
    assert pol.lock_for(33) == timedelta(seconds=2**32)
    assert pol.lock_for(34) == timedelta(seconds=2**32)
    assert pol.lock_for(500) == timedelta(seconds=2**32)


def test_retry_after_rounds_up_and_is_at_least_one() -> None:
    f = AuthFailure(
        key="k", count=5, last_failure_at=NOW, locked_until=NOW + timedelta(milliseconds=500)
    )
    assert LockoutPolicy.retry_after(f, NOW) == 1
    f2 = AuthFailure(
        key="k",
        count=5,
        last_failure_at=NOW,
        locked_until=NOW + timedelta(seconds=10, microseconds=1),
    )
    assert LockoutPolicy.retry_after(f2, NOW) == 11
    assert LockoutPolicy.retry_after(f, NOW + timedelta(milliseconds=500)) == 0
    assert LockoutPolicy.retry_after(None, NOW) == 0


# ---- identifiers, epochs and addresses ----------------------------------------------------------


def test_identifier_constants() -> None:
    assert auth.IDENTIFIER_KEY_BYTES == 32
    assert auth.IDENTIFIER_EPOCH_SECONDS == 7_776_000
    assert auth.SECURITY_LOG_RETENTION == timedelta(days=90)
    assert auth.IDENTIFIER_KEY_OVERLAP == timedelta(seconds=3600)
    assert auth._UNIX_EPOCH == datetime(1970, 1, 1, tzinfo=UTC)
    assert auth.MAX_COUNT == 1_000_000


def test_new_identifier_key_is_32_random_bytes() -> None:
    a, b = auth.new_identifier_key(), auth.new_identifier_key()
    assert isinstance(a, bytes) and len(a) == 32 and a != b


def test_identifier_epoch_boundaries_and_zone_rules() -> None:
    first = datetime(1970, 4, 1, tzinfo=UTC)  # 90 days after the Unix epoch
    assert auth.identifier_epoch(datetime(1970, 1, 1, tzinfo=UTC)) == 0
    assert auth.identifier_epoch(first - timedelta(seconds=1)) == 0
    assert auth.identifier_epoch(first) == 1
    assert auth.identifier_epoch(first + timedelta(microseconds=5)) == 1
    assert auth.identifier_epoch(first + timedelta(days=90)) == 2
    assert auth.identifier_epoch(first.astimezone(timezone(timedelta(hours=5)))) == 1
    with pytest.raises(ValueError):
        auth.identifier_epoch(datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        auth.identifier_epoch(datetime(2026, 1, 1, tzinfo=_NoOffset()))


def test_epoch_start_exact() -> None:
    assert auth.epoch_start(0) == datetime(1970, 1, 1, tzinfo=UTC)
    assert auth.epoch_start(1) == datetime(1970, 4, 1, tzinfo=UTC)
    assert auth.epoch_start(10) == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(days=900)
    for e in (0, 5, 200):
        assert auth.identifier_epoch(auth.epoch_start(e)) == e


def test_email_text_normalises_valid_and_trims_invalid() -> None:
    assert auth.email_text("  A@B.Co ") == "a@b.co"
    assert auth.email_text("  Not An Email ") == "Not An Email"


def test_address_text_canonical_forms() -> None:
    assert auth.address_text(" 1.2.3.4 ") == "1.2.3.4"
    assert auth.address_text("::ffff:1.2.3.4") == "1.2.3.4"
    assert auth.address_text("2001:DB8:0:0::1") == "2001:db8::1"
    assert auth.address_text("  not-an-address ") == "not-an-address"


def test_address_key_rules() -> None:
    assert auth.address_key("1.2.3.4") == "1.2.3.4"
    assert auth.address_key("  not-an-address ") == "not-an-address"
    assert auth.address_key("::1") == "::1"
    assert auth.address_key("64:ff9b::1.2.3.4") == "1.2.3.4"
    assert auth.address_key("64:ff9b::c000:2ff") == "192.0.2.255"
    assert auth.address_key("2001:db8:1:2:3:4:5:6") == "2001:db8:1:2::/64"
    assert auth.address_key("2001:db8:1:2:ffff:ffff:ffff:ffff") == "2001:db8:1:2::/64"
    assert auth.address_key("2001:db8:1:3::1") == "2001:db8:1:3::/64"
    assert auth.address_key("::ffff:1.2.3.4") == "1.2.3.4"


def test_address_key_link_local_zone() -> None:
    assert auth.address_key("fe80::1%eth0") == "fe80::/64%eth0"
    assert auth.address_key("fe80::1%eth1") == "fe80::/64%eth1"
    assert auth.address_key("fe80::1") == "fe80::/64"
    assert auth.address_key("febf::1") == "febf::/64"
    # a zone on an address that is not link-local is not part of the key
    ip = ipaddress.IPv6Address("2001:db8::1%eth0")
    assert ip.scope_id == "eth0"
    assert auth.address_key("2001:db8::1%eth0") == "2001:db8::/64"


def test_identifier_hash_matches_the_t46_message() -> None:
    key = secrets.token_bytes(32)
    org, text = "org-é", "tëxt"
    parts = (org.encode(), b"identifier", text.encode())
    message = b"".join(len(p).to_bytes(8, "big") + p for p in parts)
    assert (
        auth.identifier_hash(key, org, text) == hmac.new(key, message, hashlib.sha256).hexdigest()
    )
    assert auth.identifier_hash(key, org, text) != auth.identifier_hash(key, org, text + "x")
    assert auth.identifier_hash(key, "ab", "c") != auth.identifier_hash(key, "a", "bc")
    for size in (0, 31, 33):
        with pytest.raises(ValueError):
            auth.identifier_hash(secrets.token_bytes(size), org, text)


def test_halt_and_coalesced_row_ids_are_exact() -> None:
    raw = "\0".join(["halt.episode", "org", "7"]).encode()
    want = hashlib.sha256(raw).hexdigest()[:32]
    assert auth.halt_row_id("halt.episode", "org", 7) == want
    assert auth.halt_row_id("halt.episode", "org", "7") == want
    assert len(want) == 32
    late = datetime(2026, 3, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5)))  # 2026-03-02 UTC
    raw2 = "\0".join(["run.start_refused", "org", "2026-03-02", "why"]).encode()
    assert (
        auth.coalesced_row_id("run.start_refused", "org", late, "why")
        == (hashlib.sha256(raw2).hexdigest()[:32])
    )


# ---- security log -------------------------------------------------------------------------------


def _row(**kw: Any) -> SecurityLogEntry:
    base: dict[str, Any] = {
        "org_id": "org",
        "id": "0" * 32,
        "at": NOW,
        "type": "auth.login_failed",
        "expires_at": NOW + timedelta(days=90),
    }
    base.update(kw)
    return SecurityLogEntry(**base)


def test_security_entry_limits_and_frozen() -> None:
    row = _row()
    assert row.at == NOW and row.expires_at == NOW + timedelta(days=90)
    with pytest.raises(ValidationError):
        row.type = "a.b"
    assert _row(type="a." + "b" * 62).type == "a." + "b" * 62
    with pytest.raises(ValidationError):
        _row(type="a." + "b" * 63)
    assert _row(actor="a" * 200).actor == "a" * 200
    with pytest.raises(ValidationError):
        _row(actor="a" * 201)
    assert _row(subject="s" * 200).subject == "s" * 200
    with pytest.raises(ValidationError):
        _row(subject="s" * 201)
    with pytest.raises(ValidationError):
        _row(at=datetime(2026, 1, 1))
    with pytest.raises(ValidationError):
        _row(org_id="Bad Org")


def test_security_details_truncate_strings_and_keep_other_values() -> None:
    row = _row(details={"user_agent": "u" * 600, "reason": "r" * 512, "account_failures": 3})
    assert row.details["user_agent"] == "u" * 512
    assert row.details["reason"] == "r" * 512
    assert row.details["account_failures"] == 3
    assert _row(details={"via": None}).details == {"via": None}
    with pytest.raises(ValidationError):
        _row(details={"password": "x"})


def test_security_count_rules() -> None:
    coalesced = "content.capture_refused"
    assert _row(type=coalesced, details={"count": 1}).details["count"] == 1
    assert _row(type=coalesced, details={"count": 1_000_000}).details["count"] == 1_000_000
    assert _row(type=coalesced).details == {}
    for bad in (0, -1, 1_000_001, True, 5.0, "5"):
        with pytest.raises(ValidationError):
            _row(type=coalesced, details={"count": bad})
    with pytest.raises(ValidationError):
        _row(details={"count": 2})  # not a coalesced type
    for name in auth.COALESCED_TYPES:
        assert _row(type=name, details={"count": 2}).details == {"count": 2}


def test_security_entry_new() -> None:
    row = SecurityLogEntry.new(
        "org",
        NOW,
        "auth.login_failed",
        actor="human:x",
        subject="human:y",
        details={"email": "a@b.co", "via": None, "client_failures": 2},
    )
    assert isinstance(row, SecurityLogEntry)
    assert row.details == {"email": "a@b.co", "client_failures": 2}
    assert row.expires_at == NOW + timedelta(days=90)
    assert len(row.id) == 32 and int(row.id, 16) >= 0
    assert (row.actor, row.subject) == ("human:x", "human:y")
    assert SecurityLogEntry.new("org", NOW, "a.b").details == {}
    assert SecurityLogEntry.new("org", NOW, "a.b").id != SecurityLogEntry.new("org", NOW, "a.b").id


# ---- api tokens ---------------------------------------------------------------------------------


def test_api_token_constants_and_shape_check() -> None:
    assert auth.MAX_CLI_TOKEN_LIFETIME == timedelta(days=30)
    assert auth.API_TOKEN_LIFETIME == timedelta(days=90)
    assert auth.MAX_API_TOKEN_LIFETIME == timedelta(days=365)
    assert auth.MAX_API_TOKEN_LENGTH == 256
    assert auth.looks_like_api_token("nmt_" + "a" * 252) is True
    assert auth.looks_like_api_token("nmt_" + "a" * 253) is False
    assert auth.looks_like_api_token("nmt_") is False
    assert auth.looks_like_api_token("nmt_x") is True
    assert auth.looks_like_api_token("xmt_xxxxxxxx") is False
    assert auth.looks_like_api_token("") is False


def test_token_name_problem_returns_none_for_good_names() -> None:
    assert auth.token_name_problem("ci-bot_1") is None
    assert auth.token_name_problem("cli") is not None
    assert auth.token_name_problem("tok_abc") is not None
    assert auth.token_name_problem("Bad Name") is not None


def _api(**kw: Any) -> ApiToken:
    base: dict[str, Any] = {
        "org_id": "o",
        "id": "tok_" + "ab" * 12,
        "name": "bot",
        "scopes": frozenset({TokenScope.READ}),
        "token_hash": HEX64,
        "created_by": "service:cli",
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=1),
    }
    base.update(kw)
    return ApiToken(**base)


def test_api_token_org_bounds_and_permissions() -> None:
    for bad in ("", "x" * 129):
        with pytest.raises(ValidationError):
            _api(org_id=bad)
    for good in ("x", "x" * 128):
        assert _api(org_id=good).org_id == good
    with pytest.raises(ValidationError):
        _api().name = "other"
    assert _api().permissions() == frozenset({Permission.READ})
    both = _api(scopes=frozenset(TokenScope)).permissions()
    assert both == frozenset({Permission.READ, Permission.RUN})
    assert _api().actor == "service:tok_" + "ab" * 12
    assert "ab" * 32 not in repr(_api())


def test_api_token_active_window() -> None:
    t = _api()
    assert t.active(NOW) is True
    assert t.active(NOW + timedelta(days=1)) is False
    assert t.active(NOW + timedelta(days=1, seconds=-1)) is True


# ---- agents -------------------------------------------------------------------------------------


def test_agent_constants() -> None:
    assert auth.AGENT_CREDENTIAL_PREFIX == "nma_"
    assert auth.AGENT_CREDENTIAL_ID_PREFIX == "agc_"
    assert auth.MAX_AGENT_CREDENTIAL_LENGTH == 256
    assert auth.AGENT_CREDENTIAL_LIFETIME == timedelta(days=30)
    assert auth.MAX_AGENT_CREDENTIAL_LIFETIME == timedelta(days=90)
    assert auth.MIN_AGENT_CREDENTIAL_LIFETIME == timedelta(days=1)
    assert auth.AGENT_CREDENTIAL_MAX_LIVE == 5
    assert auth.AGENT_CREDENTIAL_CALLS_PER_MINUTE == 600
    assert auth.AGENT_FAILURES_PER_MINUTE == 30
    assert auth.AGENT_ROTATION_GRACE_MAX == 86_400
    assert auth.AGENT_CREDENTIAL_ENDED_RETENTION == timedelta(days=90)


def test_agent_slug_actor_and_credential_id_shapes() -> None:
    assert auth.agent_actor("writer_1") == "agent:writer_1"
    assert auth.is_agent_slug("writer") is True
    assert auth.is_agent_slug("Writer") is False
    assert auth.is_agent_slug("") is False
    assert auth.is_agent_credential_id("agc_" + "0f" * 12) is True
    assert auth.is_agent_credential_id("agc_" + "0f" * 11) is False
    assert auth.is_agent_credential_id("tok_" + "0f" * 12) is False
    with pytest.raises(ValueError):
        auth.agent_actor("Bad Id")


def test_team_key_rules() -> None:
    assert auth.team_key("Foo Bar") == "foo-bar"
    assert auth.team_key("") == "t"
    assert auth.team_key("_x") == "t_x"
    assert auth.team_key("-abc") == "t-abc"
    assert auth.team_key("9lives") == "9lives"
    assert auth.team_key("x" * 100) == "x" * 63
    assert auth.team_key("_" + "a" * 100) == "t_" + "a" * 61
    assert len(auth.team_key("_" + "a" * 100)) == 63
    assert auth.team_key("é") == "t-"


def test_agent_fingerprint_is_the_documented_json_hash() -> None:
    agent = SimpleNamespace(
        id="a1", role="écrivain", reports_to=None, model=None, checks=["z", "a"]
    )
    role = SimpleNamespace(kind=SimpleNamespace(value="worker"))
    text = (
        '{"checks":["z","a"],"id":"a1","model":null,"reports_to":null,'
        '"role":"écrivain","role_kind":"worker"}'
    )
    assert auth.agent_fingerprint(agent, role) == hashlib.sha256(text.encode()).hexdigest()
    assert json.dumps({"b": 1, "a": 2}, sort_keys=True) == '{"a": 2, "b": 1}'


def _agent_cred(**kw: Any) -> AgentCredential:
    base: dict[str, Any] = {
        "org_id": "o",
        "id": "agc_" + "ab" * 12,
        "agent_id": "writer",
        "team": "team1",
        "fingerprint": HEX64,
        "scopes": frozenset({TokenScope.RUN}),
        "token_hash": "cd" * 32,
        "created_by": UID,
        "created_at": NOW,
        "expires_at": NOW + timedelta(days=30),
    }
    base.update(kw)
    return AgentCredential(**base)


def test_agent_credential_model_rules() -> None:
    rec = _agent_cred()
    with pytest.raises(ValidationError):
        rec.team = "x"
    for bad in ("", "x" * 129):
        with pytest.raises(ValidationError):
            _agent_cred(org_id=bad)
    for good in ("x", "x" * 128):
        assert _agent_cred(org_id=good).org_id == good
    assert _agent_cred(scopes=frozenset({TokenScope.READ})).scopes == {TokenScope.READ}
    with pytest.raises(ValidationError):
        _agent_cred(scopes=frozenset())
    assert "cd" * 32 not in repr(rec)
    assert rec.actor == "agent:writer"
    assert rec.permissions() == frozenset({Permission.READ, Permission.RUN})


def test_agent_credential_time_and_revocation_consistency() -> None:
    with pytest.raises(ValidationError):
        _agent_cred(expires_at=NOW)
    with pytest.raises(ValidationError):
        _agent_cred(expires_at=NOW - timedelta(seconds=1))
    _agent_cred(expires_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValidationError):
        _agent_cred(revoked_at=NOW)  # no revoked_by
    with pytest.raises(ValidationError):
        _agent_cred(revoked_by=UID)  # no revoked_at
    assert _agent_cred(revoked_at=NOW, revoked_by=UID).revoked_by == UID
    with pytest.raises(ValidationError):
        _agent_cred(created_at=datetime(2026, 1, 1), expires_at=datetime(2026, 2, 1))


def test_agent_credential_active_and_ended_at() -> None:
    rec = _agent_cred()
    end = NOW + timedelta(days=30)
    assert rec.active(NOW) is True
    assert rec.active(end - timedelta(seconds=1)) is True
    assert rec.active(end) is False
    early = NOW + timedelta(days=1)
    revoked = _agent_cred(revoked_at=early, revoked_by="system")
    assert revoked.active(NOW) is False
    assert revoked.ended_at() == early
    assert rec.ended_at() == end
    late = _agent_cred(revoked_at=end + timedelta(days=1), revoked_by="system")
    assert late.ended_at() == end


def _issue_agent(**kw: Any) -> auth.IssuedAgentCredential:
    args: dict[str, Any] = {
        "team": "team1",
        "fingerprint": HEX64,
        "scopes": {TokenScope.RUN},
        "created_by": UID,
        "now": NOW,
    }
    args.update(kw)
    return auth.issue_agent_credential("o", "writer", **args)


def test_issue_agent_credential_shape_and_lifetime_bounds() -> None:
    issued = _issue_agent()
    assert issued.credential.startswith("nma_") and len(issued.credential) == 4 + 43
    assert issued.record.token_hash == auth.hash_agent_credential(issued.credential)
    assert issued.record.expires_at == NOW + timedelta(days=30)
    assert issued.record.created_at == NOW
    assert auth.is_agent_credential_id(issued.record.id)
    assert auth.is_agent_credential_id(auth.new_agent_credential_id())
    assert len(auth.new_agent_credential_id()) == 4 + 24
    assert _issue_agent(lifetime=timedelta(days=1)).record.expires_at == NOW + timedelta(days=1)
    assert _issue_agent(lifetime=timedelta(days=90)).record.expires_at == NOW + timedelta(days=90)
    for bad in (timedelta(days=1) - timedelta(seconds=1), timedelta(days=90, seconds=1)):
        with pytest.raises(ValueError):
            _issue_agent(lifetime=bad)
    with pytest.raises(ValueError):
        auth.issue_agent_credential(
            "o",
            "Bad Id",
            team="t",
            fingerprint=HEX64,
            scopes={TokenScope.READ},
            created_by=UID,
            now=NOW,
        )


def test_hash_agent_credential_is_sha256_hex() -> None:
    assert auth.hash_agent_credential("abc") == hashlib.sha256(b"abc").hexdigest()


def test_looks_like_agent_credential_bounds() -> None:
    assert auth.looks_like_agent_credential("nma_" + "a" * 252) is True
    assert auth.looks_like_agent_credential("nma_" + "a" * 253) is False
    assert auth.looks_like_agent_credential("nma_") is False
    assert auth.looks_like_agent_credential("nma_x") is True
    assert auth.looks_like_agent_credential("nmt_xxxx") is False


# ---- the per-credential call cap ----------------------------------------------------------------


def test_call_cap_limits_and_window() -> None:
    assert CallCap.WINDOW == timedelta(seconds=60)
    assert CallCap().limit == 600
    assert CallCap(10).limit == 10 and CallCap(6000).limit == 6000
    for bad in (9, 6001):
        with pytest.raises(ValueError):
            CallCap(bad)


def test_call_cap_accepts_exactly_limit_calls_then_reports_wait() -> None:
    cap = CallCap(10)
    for _ in range(10):
        assert cap.admit("c", NOW) is None
    assert cap.admit("c", NOW) == 60
    assert cap.admit("c", NOW + timedelta(seconds=10)) == 50
    assert cap.admit("c", NOW + timedelta(milliseconds=500)) == 60  # 59.5 rounds up
    assert cap.admit("c", NOW + timedelta(seconds=59, milliseconds=800)) == 1  # 0.2 -> 1
    assert cap.admit("other", NOW) is None  # per credential


def test_call_cap_window_is_half_open_and_wait_uses_oldest() -> None:
    cap = CallCap(10)
    assert cap.admit("c", NOW) is None
    for i in range(9):
        assert cap.admit("c", NOW + timedelta(seconds=10 + i)) is None
    # oldest at NOW: refused until NOW+60; the wait counts from the oldest call, not the next
    assert cap.admit("c", NOW + timedelta(seconds=20)) == 40
    assert cap.admit("c", NOW + timedelta(seconds=59, milliseconds=999)) == 1
    assert cap.admit("c", NOW + timedelta(seconds=60)) is None  # NOW left the window exactly now
    assert cap.admit("c", NOW + timedelta(seconds=60)) == 10  # next oldest is NOW+10


def test_call_cap_clock_moving_back_drops_future_stamps() -> None:
    cap = CallCap(10)
    later = NOW + timedelta(hours=1)
    assert cap.admit("c", later) is None  # a single stamp in the future
    for _ in range(9):
        assert cap.admit("c", later) is None
    assert cap.admit("c", later) is not None  # full
    assert cap.admit("c", NOW) is None  # clock moved back: the future stamps no longer count
    assert list(cap._seen["c"]) == [NOW]


def test_call_cap_clock_back_keeps_stamps_not_in_the_future() -> None:
    cap = CallCap(10)
    cap.admit("c", NOW)
    cap.admit("c", NOW + timedelta(seconds=30))
    assert cap.admit("c", NOW + timedelta(seconds=10)) is None
    assert list(cap._seen["c"]) == [NOW, NOW + timedelta(seconds=10)]


def test_call_cap_forgets_idle_credentials_only_above_the_threshold() -> None:
    assert CallCap.FORGET_ABOVE == 1024
    cap = CallCap(10)
    for i in range(1024):
        cap.admit(f"c{i}", NOW)
    later = NOW + timedelta(seconds=61)
    cap.admit("fresh", later)  # 1024 remembered: not above the threshold, nothing forgotten
    assert len(cap._seen) == 1025
    cap.admit("fresh2", later)  # 1025 remembered: idle ones are forgotten
    assert set(cap._seen) == {"fresh", "fresh2"}


def test_call_cap_forget_keeps_recent_and_drops_empty_and_exactly_idle() -> None:
    cap = CallCap(10)
    for i in range(1030):
        cap.admit(f"old{i}", NOW)
    now = NOW + timedelta(seconds=100)
    cap._seen["recent"] = deque([NOW, now - timedelta(seconds=59)])  # older first, newest recent
    cap._seen["edge"] = deque([now - timedelta(seconds=60)])  # exactly a window ago: idle
    cap._seen["justin"] = deque([now - timedelta(seconds=60) + timedelta(microseconds=1)])
    cap._seen["empty"] = deque()
    cap.admit("new", now)
    assert set(cap._seen) == {"recent", "justin", "new"}


def test_issue_api_token_lifetime_bounds() -> None:
    def mint(lifetime: timedelta) -> auth.IssuedApiToken:
        return auth.issue_api_token(
            "o", "bot", {TokenScope.READ}, created_by="service:cli", now=NOW, lifetime=lifetime
        )

    assert mint(timedelta(hours=1)).record.expires_at == NOW + timedelta(hours=1)
    assert mint(timedelta(seconds=1)).record.expires_at == NOW + timedelta(seconds=1)
    assert mint(timedelta(days=365)).record.expires_at == NOW + timedelta(days=365)
    for bad in (timedelta(0), timedelta(seconds=-1), timedelta(days=365, seconds=1)):
        with pytest.raises(ValueError):
            mint(bad)
    issued = mint(auth.API_TOKEN_LIFETIME)
    assert issued.token.startswith("nmt_") and len(issued.token) == 4 + 43
    assert issued.record.token_hash == auth.hash_api_token(issued.token)
    assert issued.record.token_hash == hashlib.sha256(issued.token.encode()).hexdigest()
