"""Mutation pins for core/rules.py, core/endpoints.py and core/email_change.py (#1277).

Every assertion is on behaviour: verdicts, detail texts, bounds exactly at / below / above the
limit, and normalised values. Data is fake or built at test time.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core import endpoints as ep
from nuramesh.core.email_change import (
    EMAIL_CHANGE_PER_HOUR,
    EMAIL_CHANGE_RETENTION,
    EMAIL_VERIFY_PREFIX,
    EmailChange,
    email_change_ended_at,
    email_change_link_record,
)
from nuramesh.core.rules import (
    MAX_PATTERN,
    MAX_TEXT,
    MISSING,
    Contains,
    LengthBetween,
    Matches,
    NotContains,
    PathEquals,
    PathExists,
    Rule,
    RuleGateConfig,
    evaluate,
    output_text,
    resolve_path,
)

# ---------------------------------------------------------------- rules: limits


def test_constants() -> None:
    assert MAX_PATTERN == 1_000
    assert MAX_TEXT == 5_000_000


@pytest.mark.parametrize("n,ok", [(0, False), (1, True), (10_000, True), (10_001, False)])
@pytest.mark.parametrize("cls,kind", [(Contains, "contains"), (NotContains, "not_contains")])
def test_text_value_length_bounds(cls: Any, kind: str, n: int, ok: bool) -> None:
    if ok:
        assert cls(type=kind, value="x" * n).value == "x" * n
    else:
        with pytest.raises(ValidationError):
            cls(type=kind, value="x" * n)


@pytest.mark.parametrize("n,ok", [(0, False), (1, True), (1_000, True), (1_001, False)])
def test_pattern_length_bounds(n: int, ok: bool) -> None:
    if ok:
        Matches(type="matches", pattern="a" * n)
    else:
        with pytest.raises(ValidationError):
            Matches(type="matches", pattern="a" * n)


def test_invalid_pattern_is_refused_with_text() -> None:
    with pytest.raises(ValidationError, match="invalid regular expression"):
        Matches(type="matches", pattern="(")


@pytest.mark.parametrize(
    "criterion,ok",
    [
        ("a", True),
        ("0a", True),
        ("a" + "b" * 62, True),
        ("a" + "b" * 63, False),
        ("A", False),
        ("-a", False),
        ("_a", False),
        ("a_b-c", True),
        ("0123456789", True),
        ("9a", True),
        ("", False),
        ("a b", False),
    ],
)
def test_criterion_shape(criterion: str, ok: bool) -> None:
    check = Contains(type="contains", value="x")
    if ok:
        assert Rule(criterion=criterion, check=check).criterion == criterion
    else:
        with pytest.raises(ValidationError):
            Rule(criterion=criterion, check=check)


@pytest.mark.parametrize(
    "path,ok",
    [
        ("a", True),
        ("_a", True),
        ("a-b_c1", True),
        ("a.b", True),
        ("a0123456789", True),
        ("a.b0123456789", True),
        ("a.b[0123456789]", True),
        ("a[0]", True),
        ("a[12].b[3]", True),
        ("a.b.c", True),
        ("1a", False),
        ("-a", False),
        ("a.", False),
        (".a", False),
        ("a..b", False),
        ("a[x]", False),
        ("a[]", False),
        ("[0]", False),
        ("a.1", False),
        ("", False),
        ("a" * 500, True),
        ("a" * 501, False),
    ],
)
def test_path_shape_and_length(path: str, ok: bool) -> None:
    if ok:
        assert PathExists(type="path_exists", path=path).path == path
    else:
        with pytest.raises(ValidationError):
            PathExists(type="path_exists", path=path)


def test_length_between_defaults_and_negative_refused() -> None:
    c = LengthBetween(type="length_between")
    assert (c.min, c.max) == (0, None)
    for bad in ({"min": -1}, {"max": -1}):
        with pytest.raises(ValidationError):
            LengthBetween(type="length_between", **bad)
    assert LengthBetween(type="length_between", max=0).max == 0
    assert LengthBetween(type="length_between", min=0).min == 0


def test_path_equals_value_must_be_canonical_json() -> None:
    with pytest.raises(ValidationError):
        PathEquals(type="path_equals", path="a", value={1, 2})
    assert PathEquals(type="path_equals", path="a", value=None).value is None


def test_rule_gate_config_needs_a_rule_and_forbids_extras() -> None:
    with pytest.raises(ValidationError):
        RuleGateConfig(rules=())
    rule = {"criterion": "c", "check": {"type": "contains", "value": "x"}}
    assert len(RuleGateConfig.model_validate({"rules": [rule]}).rules) == 1
    with pytest.raises(ValidationError):
        RuleGateConfig.model_validate({"rules": [rule], "other": 1})
    with pytest.raises(ValidationError):
        Contains(type="contains", value="x", extra=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Contains(type="contains", value="x").value = "y"


# ---------------------------------------------------------------- rules: output_text


def test_output_text_text_and_canonical_json() -> None:
    assert output_text("héllo") == "héllo"
    assert output_text({"b": 1, "a": [True, None]}) == '{"a":[true,null],"b":1}'


def test_output_text_size_limit_exactly() -> None:
    assert len(output_text("x" * MAX_TEXT)) == MAX_TEXT
    with pytest.raises(ValueError, match="over 5000000 characters"):
        output_text("x" * (MAX_TEXT + 1))


# ---------------------------------------------------------------- rules: contains


def test_contains_detail_met_and_not_met() -> None:
    met = evaluate(Contains(type="contains", value="ab"), "xxabxx")
    assert met == (True, "'ab' found in the output (6 characters)")
    miss = evaluate(Contains(type="contains", value="zz"), "xxabxx")
    assert miss == (False, "'zz' not found in the output (6 characters)")


def test_not_contains_is_the_inverse_with_the_same_wording() -> None:
    assert evaluate(NotContains(type="not_contains", value="ab"), "xxabxx") == (
        False,
        "'ab' found in the output (6 characters)",
    )
    assert evaluate(NotContains(type="not_contains", value="zz"), "xxabxx") == (
        True,
        "'zz' not found in the output (6 characters)",
    )


def test_case_sensitivity_default_and_off() -> None:
    assert evaluate(Contains(type="contains", value="AB"), "xxabxx")[0] is False
    assert evaluate(Contains(type="contains", value="AB", case_sensitive=False), "xxabxx")[0]
    assert evaluate(NotContains(type="not_contains", value="AB"), "xxabxx")[0] is True
    assert (
        evaluate(NotContains(type="not_contains", value="AB", case_sensitive=False), "xxabxx")[0]
        is False
    )
    # casefold, not lower: sharp s folds to ss
    assert evaluate(Contains(type="contains", value="SS", case_sensitive=False), "straße")[0]
    assert evaluate(Contains(type="contains", value="straße"), "STRASSE")[0] is False


def test_contains_looks_at_canonical_json_of_structured_output() -> None:
    assert evaluate(Contains(type="contains", value='"a":1'), {"a": 1}) == (
        True,
        "'\"a\":1' found in the output (7 characters)",
    )


def test_detail_shortens_long_values_at_80() -> None:
    v80, v81 = "v" * 80, "v" * 81
    d80 = evaluate(Contains(type="contains", value=v80), "x")[1]
    d81 = evaluate(Contains(type="contains", value=v81), "x")[1]
    assert d80.startswith(f"'{v80}' not found")
    assert d81.startswith("'" + "v" * 79 + "…' not found")


# ---------------------------------------------------------------- rules: matches


def test_matches_detail_position_and_search_semantics() -> None:
    check = Matches(type="matches", pattern=r"\d+")
    assert evaluate(check, "ab123") == (True, r"/\d+/ matched at position 2")
    assert evaluate(check, "123") == (True, r"/\d+/ matched at position 0")
    assert evaluate(check, "abc") == (False, r"/\d+/ didn't match the output")


def test_matches_pattern_in_detail_is_shortened() -> None:
    p80, p81 = "a" * 80, "a" * 81
    assert (
        evaluate(Matches(type="matches", pattern=p80), "b")[1] == f"/{p80}/ didn't match the output"
    )
    assert (
        evaluate(Matches(type="matches", pattern=p81), "b")[1]
        == "/" + "a" * 79 + "…/ didn't match the output"
    )


# ---------------------------------------------------------------- rules: length


@pytest.mark.parametrize("length,met", [(1, False), (2, True), (3, True), (4, True), (5, False)])
def test_length_between_is_inclusive_on_both_ends(length: int, met: bool) -> None:
    check = LengthBetween(type="length_between", min=2, max=4)
    assert evaluate(check, "x" * length) == (met, f"{length} characters (allowed 2–4)")


def test_length_between_open_ended_and_zero_max() -> None:
    open_ended = LengthBetween(type="length_between", min=3)
    assert evaluate(open_ended, "xx") == (False, "2 characters (allowed 3–∞)")
    assert evaluate(open_ended, "x" * 1000) == (True, "1000 characters (allowed 3–∞)")
    assert evaluate(LengthBetween(type="length_between", max=0), "") == (
        True,
        "0 characters (allowed 0–0)",
    )
    assert evaluate(LengthBetween(type="length_between", max=0), "x")[0] is False


# ---------------------------------------------------------------- rules: paths


DATA = {
    "files": [{"name": "a.txt", "tags": ["x", "y"]}, {"name": "b.txt"}],
    "s": "text",
    "n": None,
    "map": {"0": "zero"},
    "a-b": 1,
    "k0123456789": {"v0123456789": "digits"},
}


@pytest.mark.parametrize(
    "path,expected",
    [
        ("files[0].name", "a.txt"),
        ("files[1].name", "b.txt"),
        ("files[0].tags[1]", "y"),
        ("a-b", 1),
        ("n", None),
        ("s", "text"),
        ("k0123456789.v0123456789", "digits"),
    ],
)
def test_resolve_path_finds_values(path: str, expected: Any) -> None:
    assert resolve_path(DATA, path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "files[2]",  # exactly len: out of range
        "files[0].tags[2]",
        "files[0].missing",
        "s[0]",  # a string is not indexed
        "map[0]",  # a mapping is not indexed by position
        "files.name",  # a key on a list
        "nope",
        "n.x",
        "a-b.x",
    ],
)
def test_resolve_path_missing(path: str) -> None:
    assert resolve_path(DATA, path) is MISSING


def test_resolve_path_on_a_list_root_and_tuple() -> None:
    assert resolve_path({"t": ("a", "b")}, "t[1]") == "b"
    assert resolve_path({"t": ("a", "b")}, "t[2]") is MISSING


def test_path_exists_details() -> None:
    assert evaluate(PathExists(type="path_exists", path="files[0].name"), DATA) == (
        True,
        "files[0].name is present",
    )
    assert evaluate(PathExists(type="path_exists", path="n"), DATA) == (True, "n is present")
    assert evaluate(PathExists(type="path_exists", path="files[9]"), DATA) == (
        False,
        "nothing at files[9]",
    )
    assert evaluate(PathExists(type="path_exists", path="x"), "plain text") == (
        False,
        "nothing at x",
    )


def test_path_equals_details() -> None:
    eq = PathEquals(type="path_equals", path="files[0].name", value="a.txt")
    assert evaluate(eq, DATA) == (True, "files[0].name equals the expected value")
    ne = PathEquals(type="path_equals", path="files[0].name", value="zzz")
    assert evaluate(ne, DATA) == (False, "files[0].name is a string, not the expected value")
    miss = PathEquals(type="path_equals", path="files[7].name", value="a.txt")
    assert evaluate(miss, DATA) == (False, "nothing at files[7].name")
    nul = PathEquals(type="path_equals", path="n", value=None)
    assert evaluate(nul, DATA) == (True, "n equals the expected value")


def test_path_equals_compares_structurally_and_by_type() -> None:
    same = PathEquals(type="path_equals", path="o", value={"b": [1, 2], "a": "x"})
    assert evaluate(same, {"o": {"a": "x", "b": [1, 2]}})[0] is True
    assert evaluate(same, {"o": {"a": "x", "b": [2, 1]}})[0] is False
    string_one = PathEquals(type="path_equals", path="o", value="1")
    assert evaluate(string_one, {"o": 1})[1] == "o is a integer, not the expected value"
    assert evaluate(string_one, {"o": {"k": 1}})[1] == "o is a object, not the expected value"
    assert evaluate(string_one, {"o": True})[1] == "o is a boolean, not the expected value"


def test_path_equals_with_an_unserialisable_actual_value_is_not_met() -> None:
    check = PathEquals(type="path_equals", path="o", value=1)
    met, detail = evaluate(check, {"o": 1}.copy())
    assert met is True and detail == "o equals the expected value"
    met, _ = evaluate(check, {"o": float("nan")})
    assert met is False


# ---------------------------------------------------------------- endpoints


def test_endpoint_constants() -> None:
    assert ep.LOOPBACK_HOSTS == frozenset({"127.0.0.1", "[::1]"})
    assert ep.FREE_SOURCE == "declared free (local)"
    assert ep.REMOTE_RESIDENCY == (
        "openai-compatible: a remote endpoint can only declare global processing"
    )


@pytest.mark.parametrize(
    "endpoint,host",
    [
        ("http://EXAMPLE.com/x", "example.com"),
        ("https://Example.COM:8443/x", "example.com"),
        ("http://[::1]:80/", "[::1]"),
        ("http://[2001:DB8::1]/", "[2001:db8::1]"),
        ("http://127.0.0.1", "127.0.0.1"),
        ("http://u:p@host.test/", "host.test"),
        ("/just/a/path", None),
        ("", None),
        ("http://", None),
        ("http://[::1", None),  # malformed: urlsplit raises
    ],
)
def test_host_of(endpoint: str, host: str | None) -> None:
    assert ep.host_of(endpoint) == host


@pytest.mark.parametrize(
    "endpoint,loop",
    [
        ("http://127.0.0.1:9/v1", True),
        ("https://127.0.0.1/v1", True),
        ("HTTP://127.0.0.1/v1", True),
        ("http://[::1]:9/v1", True),
        ("https://[::1]/", True),
        ("ftp://127.0.0.1/", False),
        ("ws://127.0.0.1/", False),
        ("127.0.0.1:9/v1", False),
        ("//127.0.0.1/v1", False),
        ("http://localhost/v1", False),
        ("http://127.0.0.2/v1", False),
        ("http://0.0.0.0/", False),
        ("http://[::2]/", False),
        ("http://example.com/", False),
        ("http://[::1", False),
        ("", False),
    ],
)
def test_loopback_and_remote_are_opposites(endpoint: str, loop: bool) -> None:
    assert ep.is_loopback(endpoint) is loop
    assert ep.is_remote(endpoint) is (not loop)


@pytest.mark.parametrize(
    "endpoint,expected",
    [
        ("HTTP://Example.COM/V1/", "http://example.com/V1"),
        ("https://example.com", "https://example.com"),
        ("https://example.com/", "https://example.com"),
        ("https://example.com/a//", "https://example.com/a"),
        ("https://example.com:8443/Api/", "https://example.com:8443/Api"),
        ("http://example.com:0/x", "http://example.com:0/x"),
        ("http://[::1]:8080/v1/", "http://[::1]:8080/v1"),
        ("http://[::1]/v1", "http://[::1]/v1"),
        ("http://u:p@example.com/x?q=1#f", "http://example.com/x"),
        ("not a url", "not a url"),
        ("/path/only/", "/path/only/"),
        ("http:", "http:"),
        ("http://", "http://"),
        ("//host/x/", "//host/x/"),
        ("http://[::1", "http://[::1"),
        ("Not A URL/", "Not A URL/"),
        ("//Host/X/", "//Host/X/"),
        ("/Path/Only/", "/Path/Only/"),
    ],
)
def test_normalise(endpoint: str, expected: str) -> None:
    assert ep.normalise(endpoint) == expected


def test_residency_problem() -> None:
    assert ep.residency_problem("https://api.example.test/v1", "global") is None
    for scope in ("synthetic", "eu", "", "Global"):
        assert ep.residency_problem("https://api.example.test/v1", scope) == ep.REMOTE_RESIDENCY
    for scope in ("global", "eu", "synthetic"):
        assert ep.residency_problem("http://127.0.0.1:9/v1", scope) is None


def _expected_fingerprint(auth: str, endpoint: str, key: str | None, eid: str | None) -> str:
    text = json.dumps(
        {"auth": auth, "endpoint": endpoint, "endpoint_id": eid, "key": key},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_fingerprint_matches_an_independent_computation() -> None:
    got = ep.fingerprint("HTTPS://Api.Test/v1/", "bearer", "k-name", "ep-1")
    assert got == _expected_fingerprint("bearer", "https://api.test/v1", "k-name", "ep-1")
    assert len(got) == 64 and got == got.lower()
    # compact JSON, sorted keys: a literal pin of the exact canonical text
    raw = '{"auth":"none","endpoint":"http://127.0.0.1/v1","endpoint_id":null,"key":null}'
    assert (
        ep.fingerprint("http://127.0.0.1/v1", "none", None, None)
        == hashlib.sha256(raw.encode()).hexdigest()
    )


def test_fingerprint_is_utf8_unescaped_and_sensitive_to_every_field() -> None:
    base = ("https://api.test/v1", "bearer", "k", "e")
    f0 = ep.fingerprint(*base)
    assert f0 == ep.fingerprint("https://API.test/v1/", "bearer", "k", "e")
    assert len({f0, ep.fingerprint("https://api.test/v2", "bearer", "k", "e")}) == 2
    assert ep.fingerprint(base[0], "none", "k", "e") != f0
    assert ep.fingerprint(base[0], "bearer", "k2", "e") != f0
    assert ep.fingerprint(base[0], "bearer", None, "e") != f0
    assert ep.fingerprint(base[0], "bearer", "k", "e2") != f0
    assert ep.fingerprint(base[0], "bearer", "k", None) != f0
    umlaut = ep.fingerprint(base[0], "bearer", "schlüssel", "e")
    assert umlaut == _expected_fingerprint("bearer", base[0], "schlüssel", "e")
    escaped = hashlib.sha256(
        json.dumps(
            {"auth": "bearer", "endpoint": base[0], "endpoint_id": "e", "key": "schlüssel"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert umlaut != escaped


# ---------------------------------------------------------------- email change

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
HASH = "ab12" * 16


def change(**kw: Any) -> EmailChange:
    base: dict[str, Any] = {
        "org_id": "org-1",
        "user_id": "user-1",
        "new_email": "new@example.test",
        "token_hash": HASH,
        "deliverable": True,
        "created_at": T0,
        "expires_at": T0 + timedelta(hours=1),
    }
    return EmailChange(**{**base, **kw})


def test_email_change_constants() -> None:
    assert EMAIL_VERIFY_PREFIX == "nme_"
    assert EMAIL_CHANGE_RETENTION == timedelta(days=7)
    assert EMAIL_CHANGE_PER_HOUR == 5


@pytest.mark.parametrize("n,ok", [(62, False), (63, False), (64, True), (65, False)])
def test_token_hash_length(n: int, ok: bool) -> None:
    if ok:
        assert change(token_hash="a" * n).token_hash == "a" * n
    else:
        with pytest.raises(ValidationError, match="64 lowercase hex digits"):
            change(token_hash="a" * n)


@pytest.mark.parametrize("bad", ["A" + "a" * 63, "g" + "a" * 63, "a" * 63 + " ", "a" * 63 + "-"])
def test_token_hash_must_be_lowercase_hex(bad: str) -> None:
    with pytest.raises(ValidationError, match="64 lowercase hex digits"):
        change(token_hash=bad)


def test_token_hash_accepts_every_hex_digit() -> None:
    value = "0123456789abcdef" * 4
    assert change(token_hash=value).token_hash == value


@pytest.mark.parametrize("n,ok", [(2, False), (3, True), (320, True), (321, False)])
def test_new_email_length_bounds(n: int, ok: bool) -> None:
    if ok:
        assert len(change(new_email="e" * n).new_email) == n
    else:
        with pytest.raises(ValidationError):
            change(new_email="e" * n)


@pytest.mark.parametrize("field", ["org_id", "user_id"])
@pytest.mark.parametrize("n,ok", [(0, False), (1, True), (128, True), (129, False)])
def test_ids_length_bounds(field: str, n: int, ok: bool) -> None:
    if ok:
        assert len(getattr(change(**{field: "i" * n}), field)) == n
    else:
        with pytest.raises(ValidationError):
            change(**{field: "i" * n})


def test_personal_data_stays_out_of_repr_and_errors() -> None:
    c = change(new_email="private.person@example.test")
    assert "private.person" not in repr(c) and HASH not in repr(c)
    with pytest.raises(ValidationError) as info:
        change(token_hash="private.person@example.test")
    assert "private.person" not in str(info.value)


def test_email_change_is_frozen_and_closed() -> None:
    c = change()
    with pytest.raises(ValidationError):
        c.new_email = "other@example.test"
    with pytest.raises(ValidationError):
        change(extra="x")


def test_ended_at_is_the_latest_of_those_present() -> None:
    assert email_change_ended_at(change()) == T0 + timedelta(hours=1)
    consumed = T0 + timedelta(hours=3)
    cancelled = T0 + timedelta(hours=2)
    assert email_change_ended_at(change(consumed_at=consumed)) == consumed
    assert email_change_ended_at(change(cancelled_at=cancelled)) == cancelled
    assert email_change_ended_at(change(consumed_at=consumed, cancelled_at=cancelled)) == consumed
    early = T0 + timedelta(minutes=5)
    assert email_change_ended_at(change(consumed_at=early)) == T0 + timedelta(hours=1)


def test_link_record_maps_every_field() -> None:
    consumed, cancelled = T0 + timedelta(minutes=1), T0 + timedelta(minutes=2)
    rec = email_change_link_record(change(consumed_at=consumed, cancelled_at=cancelled))
    assert rec.token_hash == HASH
    assert rec.prefix == "nme_"
    assert rec.expires_at == T0 + timedelta(hours=1)
    assert rec.used_at == consumed
    assert rec.revoked_at == cancelled
    fresh = email_change_link_record(change())
    assert fresh.used_at is None and fresh.revoked_at is None
