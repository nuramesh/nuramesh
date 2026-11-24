"""Token names (#634, spec/auth/token-lifecycle.md R3, R13, ST5, ST12): the input rules, the
replacement name, the model patterns and the keyed retired-name hash. Pure functions, no store."""

from datetime import UTC, datetime, timedelta

import pytest

from nuramesh.core.auth import (
    ApiToken,
    TokenScope,
    identifier_hash,
    new_token_name_problem,
    next_erased_token_name,
    token_name_hash,
    token_name_problem,
)

T0 = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
TID = "tok_0123456789abcdef01234567"
HEX = "0123456789abcdef01234567"
PATTERN_TEXT = "a token name is 1 to 63 of a-z, 0-9, _ and -, starting with a letter or digit"
RESERVED_TEXT = "that name is reserved"
TOK_TEXT = "names starting 'tok_' are reserved for token ids"


def row(name: str, created_by: str = "human:u_a1", **kw: object) -> ApiToken:
    base: dict[str, object] = {
        "org_id": "o1",
        "id": TID,
        "name": name,
        "scopes": frozenset({TokenScope.READ}),
        "token_hash": "a" * 64,
        "created_by": created_by,
        "created_at": T0,
        "expires_at": T0 + timedelta(days=1),
    }
    return ApiToken(**{**base, **kw})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("Ada Smith", PATTERN_TEXT),
        ("", PATTERN_TEXT),
        ("a" * 64, PATTERN_TEXT),
        ("ci-main\n", PATTERN_TEXT),
        ("ci-١", PATTERN_TEXT),
        ("cli", RESERVED_TEXT),
        ("system", RESERVED_TEXT),
        ("api", RESERVED_TEXT),
        ("engine", RESERVED_TEXT),
        ("erased-1", RESERVED_TEXT),
        ("erased-", RESERVED_TEXT),
        ("erased-" + HEX, RESERVED_TEXT),
        ("tok_x", TOK_TEXT),
    ],
)
def test_input_rules_in_order_and_never_echo_the_value(name: str, text: str) -> None:
    problem = new_token_name_problem(name)
    assert problem == text
    assert name == "" or name not in problem or name in text  # no submitted value in the text


@pytest.mark.parametrize("name", ["ci-main", "a", "0x", "a_b-c", "a" * 63])
def test_a_good_name_has_no_problem(name: str) -> None:
    assert new_token_name_problem(name) is None


def test_the_stored_row_validator_is_unchanged_so_legacy_names_still_load() -> None:
    for name in ("erased-" + HEX, "erased-x", "erased-legacy", "erased-" + HEX + "-1"):
        assert token_name_problem(name) is None  # ST5: stored rows must keep loading
        assert row(name).name == name
    with pytest.raises(ValueError):
        row("Bad Name")


@pytest.mark.parametrize(
    "who", ["erased-user", "system", "operator:ops1", "service:cli", "human:u_a1"]
)
def test_the_model_accepts_the_issuers_the_spec_names(who: str) -> None:
    assert row("ci-main", created_by=who).created_by == who
    assert row("ci-main", revoked_at=T0 + timedelta(hours=1), revoked_by=who).revoked_by == who


def test_the_model_refuses_other_issuers() -> None:
    for who in ("service:other", "erased", "agent:x"):
        with pytest.raises(ValueError):
            row("ci-main", created_by=who)


def test_the_replacement_name_and_its_collision_suffix() -> None:
    first = "erased-" + HEX
    assert next_erased_token_name(TID, set()) == first
    assert next_erased_token_name(TID, {first}) == first + "-1"
    assert next_erased_token_name(TID, {first, first + "-1"}) == first + "-2"
    assert next_erased_token_name(TID, {first + "-1"}) == first  # only the exact names collide


@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("erased-" + HEX, True),
        ("erased-" + HEX + "-1", True),
        ("erased-" + HEX + "-12", True),
        ("erased-" + HEX + "-0", False),
        ("erased-" + HEX + "-01", False),
        ("erased-" + HEX + "-x", False),
        ("erased-aaaaaaaaaaaaaaaaaaaaaaaa", False),  # another token's erased- name is replaced
        ("ada-smith-laptop", False),
    ],
)
def test_er23_er26_idempotent_clause(name: str, kept: bool) -> None:
    taken = {name}
    chosen = next_erased_token_name(TID, taken - {name}, current=name)
    assert (chosen == name) is kept


def test_st12_the_retired_name_hash_differs_from_the_identifier_hash() -> None:
    key = b"\x07" * 32  # obviously fake, one repeated byte
    same_text = "ada-smith-laptop"
    assert token_name_hash(key, "o1", same_text) != identifier_hash(key, "o1", same_text)
    assert token_name_hash(key, "o1", same_text) == token_name_hash(key, "o1", same_text)
    assert token_name_hash(key, "o1", same_text) != token_name_hash(key, "o2", same_text)
    assert token_name_hash(key, "o1", same_text) != token_name_hash(b"\x08" * 32, "o1", same_text)
    assert len(token_name_hash(key, "o1", same_text)) == 64
