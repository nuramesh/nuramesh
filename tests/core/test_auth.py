"""Users, password hashes and the setup token (R1.22 PR 1, T64; spec/auth/README.md)."""

import base64
import hashlib
import hmac
import random
import re
import resource
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core import auth
from nuramesh.core.auth import (
    DEFAULT_PARAMS,
    Credential,
    PasswordRuleError,
    Role,
    ScryptParams,
    SetupToken,
    SetupTokenCheck,
    User,
    check_setup_token,
    erased_copy,
    hash_password,
    hash_setup_token,
    issue_setup_token,
    needs_rehash,
    new_user_id,
    normalize_email,
    parse_hash,
    password_problem,
    verify_password,
    verify_unknown,
)
from nuramesh.core.people import is_human_id

T0 = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
FAST = ScryptParams(n=16, r=1, p=1)  # cheap parameters for tests that don't measure cost
PASSWORD = "correct horse battery"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


# ---- hash format -----------------------------------------------------------------------------


def test_the_default_parameters_are_the_measured_choice() -> None:
    assert (DEFAULT_PARAMS.n, DEFAULT_PARAMS.r, DEFAULT_PARAMS.p) == (2**15, 8, 3)
    assert (DEFAULT_PARAMS.salt_bytes, DEFAULT_PARAMS.key_bytes) == (16, 32)
    assert DEFAULT_PARAMS.memory_bytes == 128 * 8 * (2**15 + 3 + 2)  # about 32 MiB


def test_a_hash_is_six_dollar_separated_fields_with_unpadded_base64url() -> None:
    stored = hash_password(PASSWORD, FAST)
    algorithm, n, r, p, salt, key = stored.split("$")
    assert (algorithm, n, r, p) == ("scrypt", "16", "1", "1")
    assert re.fullmatch(r"[A-Za-z0-9_-]{22}", salt)  # 16 bytes
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", key)  # 32 bytes


def test_the_hash_is_the_documented_scrypt_of_the_nfkc_password() -> None:
    stored = hash_password("pässword-with-umlaut", FAST)
    parsed = parse_hash(stored)
    assert parsed is not None
    expected = hashlib.scrypt(
        "pässword-with-umlaut".encode(), salt=parsed.salt, n=16, r=1, p=1, dklen=32
    )
    assert parsed.key == expected


def test_a_hash_made_by_hand_from_the_spec_verifies() -> None:
    salt = bytes(range(16))
    key = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=16, r=1, p=1, dklen=32)
    assert verify_password(PASSWORD, f"scrypt$16$1$1${b64(salt)}${b64(key)}")


def test_round_trip_and_wrong_password() -> None:
    stored = hash_password(PASSWORD, FAST)
    assert verify_password(PASSWORD, stored)
    assert not verify_password(PASSWORD + "x", stored)
    assert not verify_password("", stored)


def test_unicode_is_normalised_before_hashing() -> None:
    composed, decomposed = "café-password!", "café-password!"
    assert verify_password(decomposed, hash_password(composed, FAST))


def test_every_hash_has_its_own_salt() -> None:
    assert hash_password(PASSWORD, FAST) != hash_password(PASSWORD, FAST)


def test_the_default_parameters_hash_and_verify() -> None:
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$32768$8$3$")
    assert verify_password(PASSWORD, stored)
    assert not needs_rehash(stored)


# ---- parameter upgrade ------------------------------------------------------------------------


def test_an_older_hash_still_verifies_and_asks_to_be_replaced() -> None:
    old = hash_password(PASSWORD, FAST)
    assert verify_password(PASSWORD, old)  # old hashes keep working under new settings
    assert needs_rehash(old, DEFAULT_PARAMS)
    assert not needs_rehash(old, FAST)
    upgraded = hash_password(PASSWORD, ScryptParams(n=32, r=2, p=1))  # the login's re-hash
    assert verify_password(PASSWORD, upgraded)
    assert not needs_rehash(upgraded, ScryptParams(n=32, r=2, p=1))
    assert needs_rehash(upgraded, FAST)


def test_other_sizes_of_salt_or_key_also_ask_for_a_rehash() -> None:
    stored = hash_password(PASSWORD, ScryptParams(n=16, r=1, p=1, salt_bytes=32, key_bytes=64))
    assert verify_password(PASSWORD, stored)
    assert needs_rehash(stored, FAST)


def test_an_unparsable_hash_needs_a_rehash() -> None:
    assert needs_rehash("argon2id$whatever")


# ---- constant-time comparison and the unknown-user path ---------------------------------------


class Spy:
    """Records calls of a function and passes them through."""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return self.real(*args, **kwargs)


def test_passwords_are_compared_with_hmac_compare_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = hash_password(PASSWORD, FAST)
    parsed = parse_hash(stored)
    assert parsed is not None
    spy = Spy(hmac.compare_digest)
    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert verify_password(PASSWORD, stored)
    assert len(spy.calls) == 1
    (derived, stored_key), _ = spy.calls[0]
    assert stored_key == parsed.key and derived == parsed.key
    assert not verify_password("another password", stored)
    assert len(spy.calls) == 2


def test_setup_tokens_are_compared_with_hmac_compare_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = issue_setup_token("acme", T0)
    spy = Spy(hmac.compare_digest)
    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert check_setup_token(issued.token, issued.record, T0) is SetupTokenCheck.OK
    assert len(spy.calls) == 1


def test_a_wrong_password_and_an_unknown_user_take_the_same_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = hash_password(PASSWORD, FAST)
    scrypt, compare = Spy(hashlib.scrypt), Spy(hmac.compare_digest)
    monkeypatch.setattr(hashlib, "scrypt", scrypt)
    monkeypatch.setattr(hmac, "compare_digest", compare)
    auth._dummy_hash.cache_clear()
    auth._dummy_hash(FAST)  # building the stand-in hash is a one-off; not part of the comparison
    scrypt.calls.clear()
    compare.calls.clear()

    assert verify_password("wrong password!!", stored) is False
    wrong = (list(scrypt.calls), len(compare.calls))
    scrypt.calls.clear()
    compare.calls.clear()
    assert verify_unknown("wrong password!!", FAST) is False
    unknown = (list(scrypt.calls), len(compare.calls))

    assert wrong[1] == unknown[1] == 1
    assert len(wrong[0]) == len(unknown[0]) == 1
    for key in ("n", "r", "p", "maxmem", "dklen"):  # same cost parameters
        assert wrong[0][0][1][key] == unknown[0][0][1][key]
    auth._dummy_hash.cache_clear()


def test_verify_unknown_never_succeeds() -> None:
    assert verify_unknown("anything at all", FAST) is False
    assert verify_unknown("dummy password for unknown users", FAST) is False


# ---- memory ----------------------------------------------------------------------------------


def test_maxmem_is_always_above_what_scrypt_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    for params in (DEFAULT_PARAMS, ScryptParams(n=2**17, r=8, p=1), FAST):
        assert params.maxmem > 128 * params.n * params.r
        assert params.maxmem >= params.memory_bytes
    scrypt = Spy(hashlib.scrypt)
    monkeypatch.setattr(hashlib, "scrypt", scrypt)
    hash_password(PASSWORD, ScryptParams(n=2**17, r=8, p=1))  # fails at exactly 128 MiB
    assert scrypt.calls[0][1]["maxmem"] > 128 * 2**17 * 8


PEAK_SCRIPT = """
import resource, sys
from nuramesh.core.auth import hash_password, verify_password
def rss():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024
before = rss()
stored = hash_password("correct horse battery")
assert verify_password("correct horse battery", stored)
print(rss() - before)
"""


def test_hashing_with_the_default_parameters_stays_near_32_mib() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PEAK_SCRIPT], capture_output=True, text=True, check=True
    )
    delta = int(result.stdout.strip())
    assert delta < 48 * 2**20, f"peak grew by {delta / 2**20:.0f} MiB"
    assert resource.getpagesize() > 0


def test_parameters_that_would_need_too_much_memory_are_refused() -> None:
    with pytest.raises(ValidationError):
        ScryptParams(n=2**20, r=32, p=1)  # 4 GiB
    stored = f"scrypt$1048576$32$1${b64(bytes(16))}${b64(bytes(32))}"
    assert parse_hash(stored) is None  # a tampered row can't make the server allocate it
    assert verify_password(PASSWORD, stored) is False


@pytest.mark.parametrize("n", [0, 1, 3, 100, 2**21])
def test_n_must_be_a_power_of_two_in_range(n: int) -> None:
    with pytest.raises(ValidationError):
        ScryptParams(n=n)


# ---- password rules ---------------------------------------------------------------------------


def test_the_length_rules() -> None:
    assert password_problem("") is not None
    assert password_problem("a" * 11) is not None
    assert password_problem("a" * 12) is None
    assert password_problem("a" * 1024) is None
    assert password_problem("a" * 1025) is not None
    for bad in ("", "short", "a" * 1025):
        with pytest.raises(PasswordRuleError):
            hash_password(bad, FAST)


def test_length_is_counted_in_characters_after_normalisation() -> None:
    assert password_problem("é" * 12) is None  # 12 characters, 24 bytes
    assert password_problem("é" * 6) is not None  # decomposed pairs fold to 6 characters


def test_an_overlong_password_is_refused_before_any_hashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = hash_password(PASSWORD, FAST)
    scrypt = Spy(hashlib.scrypt)
    monkeypatch.setattr(hashlib, "scrypt", scrypt)
    assert verify_password("a" * 5000, stored) is False
    assert verify_password("a" * 1025, stored) is False
    assert scrypt.calls == []


# ---- the parser never crashes ----------------------------------------------------------------


GOOD_SALT, GOOD_KEY = b64(bytes(16)), b64(bytes(32))


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "$",
        "scrypt",
        "scrypt$16$1$1$$",
        f"scrypt$16$1$1${GOOD_SALT}",  # five fields
        f"scrypt$16$1$1${GOOD_SALT}${GOOD_KEY}$extra",  # seven fields
        f"argon2id$16$1$1${GOOD_SALT}${GOOD_KEY}",  # another algorithm
        f"SCRYPT$16$1$1${GOOD_SALT}${GOOD_KEY}",
        f"scrypt$016$1$1${GOOD_SALT}${GOOD_KEY}",  # leading zero
        f"scrypt$+16$1$1${GOOD_SALT}${GOOD_KEY}",
        f"scrypt$-16$1$1${GOOD_SALT}${GOOD_KEY}",
        f"scrypt$16.0$1$1${GOOD_SALT}${GOOD_KEY}",
        f"scrypt$ 16$1$1${GOOD_SALT}${GOOD_KEY}",
        f"scrypt$١٦$1$1${GOOD_SALT}${GOOD_KEY}",  # Arabic-Indic digits
        f"scrypt$15$1$1${GOOD_SALT}${GOOD_KEY}",  # not a power of two
        f"scrypt$16$0$1${GOOD_SALT}${GOOD_KEY}",
        f"scrypt$16$1$0${GOOD_SALT}${GOOD_KEY}",
        f"scrypt$16$1$17${GOOD_SALT}${GOOD_KEY}",  # p above the limit
        f"scrypt$16$1$1${GOOD_SALT}=${GOOD_KEY}",  # padded
        f"scrypt$16$1$1${GOOD_SALT[:-1]}${GOOD_KEY}",  # bad length
        f"scrypt$16$1$1${GOOD_SALT[:-1]}+${GOOD_KEY}",  # standard alphabet
        f"scrypt$16$1$1$ {GOOD_SALT[1:]}${GOOD_KEY}",
        f"scrypt$16$1$1${b64(bytes(7))}${GOOD_KEY}",  # salt under 8 bytes
        f"scrypt$16$1$1${GOOD_SALT}${b64(bytes(15))}",  # hash under 16 bytes
        f"scrypt$16$1$1${GOOD_SALT}${b64(bytes(65))}",
        f"scrypt$99999999999$1$1${GOOD_SALT}${GOOD_KEY}",
        "scrypt$16$1$1$\x00$\x00",
        "scrypt$16$1$1$é$é",
    ],
)
def test_malformed_hashes_are_refused_not_raised(stored: str) -> None:
    assert parse_hash(stored) is None
    assert verify_password(PASSWORD, stored) is False
    assert verify_unknown(PASSWORD, FAST) is False


def test_a_non_canonical_base64_tail_is_refused() -> None:
    # 22 characters decode to 16 bytes, but the last character must have its spare bits zero
    assert GOOD_SALT.endswith("A")
    assert parse_hash(f"scrypt$16$1$1${GOOD_SALT[:-1]}B${GOOD_KEY}") is None


def test_random_junk_never_crashes_the_parser_or_verifier() -> None:
    rng = random.Random(20261025)
    alphabet = "scrypt$0123456789-_=+/ AZaz\x00é١\n"
    good = hash_password(PASSWORD, FAST)
    for _ in range(3000):
        junk = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        assert parse_hash(junk) is None or junk.count("$") == 5
        assert verify_password(PASSWORD, junk) is False or parse_hash(junk) is not None
    for _ in range(500):  # one-character mutations of a good hash
        chars = list(good)
        chars[rng.randrange(len(chars))] = rng.choice(alphabet)
        mutated = "".join(chars)
        if mutated != good:
            assert verify_password(PASSWORD, mutated) is False
    parsed = parse_hash(good)
    assert parsed is not None and parsed.params == FAST


def test_a_hash_with_different_key_bytes_fails_to_verify() -> None:
    stored = hash_password(PASSWORD, FAST)
    other = stored.rsplit("$", 1)[0] + "$" + b64(bytes(32))
    assert parse_hash(other) is not None
    assert verify_password(PASSWORD, other) is False


# ---- users ------------------------------------------------------------------------------------


def user(**changes: Any) -> User:
    data: dict[str, Any] = {
        "org_id": "acme",
        "id": new_user_id(),
        "email": "Ada@Example.org",
        "display_name": "Ada Example",
        "role": Role.OWNER,
        "created_at": T0,
    }
    data.update(changes)
    return User(**data)


def test_user_ids_are_opaque_human_ids() -> None:
    ids = {new_user_id() for _ in range(50)}
    assert len(ids) == 50
    for value in ids:
        assert re.fullmatch(r"human:u_[0-9a-f]{20}", value) and is_human_id(value)


def test_roles() -> None:
    assert [r.value for r in Role] == ["owner", "admin", "builder", "approver", "viewer"]
    with pytest.raises(ValidationError):
        user(role="superuser")


@pytest.mark.parametrize("bad", ["ada@example.org", "ada", "human:", "service:x", "human:a.b"])
def test_a_user_id_must_be_a_human_id(bad: str) -> None:
    with pytest.raises(ValidationError):
        user(id=bad)


def test_email_is_normalised_trimmed_and_lower_cased() -> None:
    assert user().email == "ada@example.org"
    assert normalize_email("  ADA@Example.ORG ") == "ada@example.org"
    assert normalize_email("ａｄａ@example.org") == "ada@example.org"  # NFKC
    assert normalize_email("a+tag@Sub.Example.org") == "a+tag@sub.example.org"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " ",
        "ada",
        "ada@",
        "@example.org",
        "a@b@example.org",
        "ada@example",
        "ada@.org",
        "ada@example.",
        "ada@exa..mple.org",
        "ada @example.org",
        "ada@exam\nple.org",
        "ada\x00@example.org",
        "a" * 250 + "@example.org",
    ],
)
def test_bad_emails_are_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        normalize_email(bad)


def test_personal_data_stays_out_of_repr() -> None:
    text = repr(user())
    assert "example.org" not in text and "Ada" not in text and "human:u_" in text


def test_an_erased_user_keeps_the_id_and_loses_the_personal_data() -> None:
    original = user()
    gone = erased_copy(original, T0 + timedelta(days=1))
    assert gone.erased and gone.id == original.id and gone.role == original.role
    assert gone.email is None and gone.display_name is None
    assert gone.created_at == original.created_at
    with pytest.raises(ValidationError):
        user(email=None)  # not erased: needs an email
    with pytest.raises(ValidationError):
        user(erased_at=T0)  # erased users keep no email


def test_times_need_a_time_zone() -> None:
    with pytest.raises(ValidationError):
        user(created_at=datetime(2026, 1, 1))


def test_a_credential_must_hold_a_well_formed_hash_and_hides_it() -> None:
    stored = hash_password(PASSWORD, FAST)
    credential = Credential(
        org_id="acme", user_id=new_user_id(), password_hash=stored, updated_at=T0
    )
    assert stored not in repr(credential)
    with pytest.raises(ValidationError):
        Credential(org_id="acme", user_id=new_user_id(), password_hash="plain", updated_at=T0)


# ---- the setup token --------------------------------------------------------------------------


def test_a_setup_token_is_256_random_bits_and_only_its_hash_is_kept() -> None:
    issued = issue_setup_token("acme", T0)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", issued.token)  # 32 bytes, unpadded
    record = issued.record
    assert record.token_hash == hashlib.sha256(issued.token.encode()).hexdigest()
    assert record.token_hash == hash_setup_token(issued.token)
    assert issued.token not in record.model_dump_json()
    assert record.created_at == T0 and record.expires_at == T0 + timedelta(hours=24)
    assert record.consumed_at is None
    assert issue_setup_token("acme", T0).token != issued.token


def test_the_token_never_shows_in_repr_or_str() -> None:
    issued = issue_setup_token("acme", T0)
    for text in (repr(issued), str(issued), repr(issued.record), str(issued.record)):
        assert issued.token not in text


def test_checking_a_token() -> None:
    issued = issue_setup_token("acme", T0, lifetime=timedelta(hours=1))
    record = issued.record
    check = check_setup_token
    assert check(issued.token, record, T0) is SetupTokenCheck.OK
    assert check(issued.token, record, T0 + timedelta(minutes=59)) is SetupTokenCheck.OK
    assert check(issued.token, record, T0 + timedelta(hours=1)) is SetupTokenCheck.EXPIRED
    assert check("x" * 43, record, T0) is SetupTokenCheck.WRONG
    assert check("", record, T0) is SetupTokenCheck.WRONG
    assert check("x" * 1000, record, T0) is SetupTokenCheck.WRONG
    used = record.model_copy(update={"consumed_at": T0})
    assert check(issued.token, used, T0) is SetupTokenCheck.USED
    assert check("wrong", used, T0) is SetupTokenCheck.WRONG  # a wrong token learns nothing


def test_the_lifetime_must_be_positive() -> None:
    with pytest.raises(ValueError):
        issue_setup_token("acme", T0, lifetime=timedelta(0))


def test_lone_surrogates_are_not_passwords_or_tokens() -> None:
    stored = hash_password("correct horse battery staple")
    assert verify_password("\ud800" * 12, stored) is False
    assert verify_unknown("\ud800" * 12) is False
    record = SetupToken(
        org_id="acme",
        token_hash="0" * 64,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        expires_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert check_setup_token("\ud800", record, datetime(2026, 1, 1, 1, tzinfo=UTC)) is (
        SetupTokenCheck.WRONG
    )


def test_a_malformed_stored_credential_does_not_leak_hash_material_in_its_error() -> None:
    bad = "scrypt$15$8$3$AAAAAAAAAAAAAAAAAAAAAA$" + "Z" * 10 + "SECRETSECRETSECRET"
    with pytest.raises(ValidationError) as caught:
        Credential(
            user_id="human:u_abc", org_id="acme", password_hash=bad, updated_at=datetime.now(UTC)
        )
    assert "SECRETSECRET" not in str(caught.value)
