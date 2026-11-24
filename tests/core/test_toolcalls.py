"""The pure rules of the adapter tool-call contract (spec/adapters/README.md, S4): tool names,
call ids, readable arguments, the arguments digest, the manifest and `tool_set_hash`, reason
codes, the fixed texts and the option ranges. Test names carry the spec's rule and case ids."""

import json
import re

import pytest

from nuramesh.core.toolcalls import (
    DEADLINE_DURABLE_SECONDS,
    DEADLINE_WAIT_SECONDS,
    DENIAL_LIMIT,
    MAX_ARGUMENTS_TEXT_BYTES,
    MAX_CANONICAL_BYTES,
    ManifestEntry,
    ManifestError,
    ReasonCode,
    UnreadableArguments,
    UnreadableToolName,
    ask_text,
    call_id_for,
    check_deadline_seconds,
    check_denial_limit,
    check_tool_name,
    deny_text,
    freeze_manifest,
    is_soft_denial,
    is_stop,
    read_arguments,
    schema_hash,
)

EMPTY = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
DEPLOY_SCHEMA = "9dcd5a0b8da1211f414d50c369f185c0a71edf170846dab899deb66a086cf263"


# --- AD-10: the fixed vectors -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "canonical", "digest"),
    [
        ({}, b"{}", EMPTY),
        (
            {"replicas": 2, "env": "staging"},
            b'{"env":"staging","replicas":2}',
            "bdc07043835c74f713c3982be1c831f1a994bb86dbee7bef4b75f23ac16dd2bf",
        ),
        (
            {"env": "prod", "replicas": 2},
            b'{"env":"prod","replicas":2}',
            "72b8f3b5998bfbe98c8eb62116665be3633116cee92549d9de321229ba616d4b",
        ),
        (
            {"b": [1, 2.0, 1.5, True, None], "a": {"z": "é", "y": ""}},
            '{"a":{"y":"","z":"é"},"b":[1,2,1.5,true,null]}'.encode(),
            "170d2939d5250b56b16036ac46bf7740380206ee0e508b50d5f2414d7f32e4de",
        ),
    ],
)
def test_ad10_vectors(arguments: dict[str, object], canonical: bytes, digest: str) -> None:
    read = read_arguments(arguments)
    assert read.canonical == canonical
    assert read.digest == digest


def test_ad10_c07_key_order_and_whitespace_do_not_change_the_digest() -> None:
    a = read_arguments('{"replicas": 2, "env": "staging"}')
    b = read_arguments('  {"env":"staging",\n"replicas":2.0}')
    c = read_arguments({"env": "staging", "replicas": 2})
    assert a.digest == b.digest == c.digest


def test_ad10_c08_different_arguments_different_digest() -> None:
    assert read_arguments({"env": "prod"}).digest != read_arguments({"env": "staging"}).digest


# --- AD-9: what is readable ---------------------------------------------------------------------


def test_ad9_c12_empty_text_is_the_empty_object() -> None:
    read = read_arguments("")
    assert read.value == {} and read.digest == EMPTY
    assert read_arguments(b"").digest == EMPTY


@pytest.mark.parametrize(
    "text",
    [
        "[1]",  # C12: not an object
        "{not json",  # C12: not JSON
        '"text"',
        "1",
        "null",
        " ",  # only empty text is the empty object
        '{"env":"staging","env":"prod"}',  # C09: duplicate key
        '{"a":{"k":1,"k":2}}',  # C09: duplicate key, nested
        '{"a":[{"k":1,"k":2}]}',
        '{"k":1,"\\u006b":2}',  # compared after JSON unescaping
        '{"n":NaN}',
        '{"n":Infinity}',
        '{"n":-Infinity}',
        '{"n":1e400}',  # infinite as a double
        '{"n":9007199254740992}',  # 2^53
        '{"n":-9007199254740992}',
        '{"n":1e20}',  # a float whose value is an integer outside the range
        '{"n":9007199254740992.0}',
        '{"s":"\\ud800"}',  # a lone surrogate
        '{"\\udc00":1}',
        '{"a":1} {"b":2}',
    ],
)
def test_ad9_unreadable_text(text: str) -> None:
    with pytest.raises(UnreadableArguments):
        read_arguments(text)


def test_ad9_bytes_that_are_not_utf8_are_unreadable() -> None:
    with pytest.raises(UnreadableArguments):
        read_arguments(b'{"a":"\xff"}')


@pytest.mark.parametrize(
    "value",
    [
        {"env": b"staging"},  # C10
        {"replicas": float("nan")},  # C10
        {"replicas": float("inf")},
        {"replicas": 9007199254740992},  # C10
        {"replicas": -9007199254740992},
        {"replicas": 1e20},
        {"tags": {"a", "b"}},
        {"when": object()},
        {"t": (1, 2)},  # nothing is coerced
        {1: "a"},  # a key that is not a string
        {"s": "\ud800"},
        {"\udfff": 1},
        ["not", "an", "object"],
        None,
        7,
    ],
)
def test_ad9_c10_unreadable_python_values(value: object) -> None:
    with pytest.raises(UnreadableArguments):
        read_arguments(value)


def test_ad9_c10_the_largest_safe_integer_is_readable() -> None:
    assert read_arguments({"replicas": 9007199254740991}).value == {"replicas": 9007199254740991}
    assert read_arguments('{"replicas":-9007199254740991}').value == {"replicas": -9007199254740991}
    assert read_arguments({"n": 9007199254740991.0}).canonical == b'{"n":9007199254740991}'
    with pytest.raises(UnreadableArguments):
        read_arguments({"big": 1.5e300})  # every double this large is an integer value


def _nested(levels: int) -> dict[str, object]:
    value: dict[str, object] = {}
    for _ in range(levels - 1):
        value = {"k": value}
    return value


def test_ad9_exactly_64_levels_are_readable_and_65_are_not() -> None:
    assert read_arguments(_nested(64)).value == _nested(64)
    assert read_arguments(json.dumps(_nested(64))).value == _nested(64)
    with pytest.raises(UnreadableArguments):
        read_arguments(_nested(65))
    with pytest.raises(UnreadableArguments):
        read_arguments(json.dumps(_nested(65)))
    lists: object = 1
    for _ in range(64):
        lists = [lists]
    with pytest.raises(UnreadableArguments):
        read_arguments({"a": lists})  # the object is level 1, the lists 2 to 65


def test_ad9_very_deep_text_is_a_deny_not_a_crash() -> None:
    with pytest.raises(UnreadableArguments):
        read_arguments('{"a":' * 100_000 + "1" + "}" * 100_000)
    with pytest.raises(UnreadableArguments):
        read_arguments("[" * 100_000)


def test_ad9_text_longer_than_4_mib_is_refused_before_parsing() -> None:
    pad = MAX_ARGUMENTS_TEXT_BYTES - len('{"a":""}')
    with pytest.raises(UnreadableArguments):
        # one byte over the limit, but still over the canonical limit: both are this deny
        read_arguments('{"a":"' + "x" * (pad + 1) + '"}')
    with pytest.raises(UnreadableArguments):
        read_arguments(" " * (MAX_ARGUMENTS_TEXT_BYTES + 1))
    # whitespace up to exactly the limit is parsed normally
    read = read_arguments("{}" + " " * (MAX_ARGUMENTS_TEXT_BYTES - 2))
    assert read.digest == EMPTY


def test_ad9_c11_canonical_form_of_exactly_1_mib_is_handled_one_more_is_not() -> None:
    fixed = len(b'{"a":""}')
    exact = {"a": "x" * (MAX_CANONICAL_BYTES - fixed)}
    assert len(read_arguments(exact).canonical) == MAX_CANONICAL_BYTES
    with pytest.raises(UnreadableArguments):
        read_arguments({"a": "x" * (MAX_CANONICAL_BYTES - fixed + 1)})


def test_ad9_ad65_the_error_never_holds_the_arguments() -> None:
    secret = "obviously-fake-argument-value"
    duplicate = '{"k":"' + secret + '","k":1}'
    for bad in (duplicate, {"k": secret, "x": b"1"}, '{"' + secret + '": NaN}'):
        with pytest.raises(UnreadableArguments) as caught:
            read_arguments(bad)
        assert secret not in str(caught.value) and secret not in repr(caught.value)
        assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_ad11_the_value_is_rebuilt_from_what_was_hashed() -> None:
    given = {"env": "staging", "nested": {"n": [1, 2]}}
    read = read_arguments(given)
    assert read.value == given and read.value is not given
    given["nested"]["n"].append(3)  # type: ignore[index]
    assert read.value == {"env": "staging", "nested": {"n": [1, 2]}}
    assert read_arguments(read.value).digest == read.digest


# --- AD-7, AD-8 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Bash", "mcp__files__read", "a.b:c-d_e", "x" * 128, "0"])
def test_ad7_names_that_match(name: str) -> None:
    assert check_tool_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "bad name!", "x" * 129, " deploy", "deploy\n", "déploy", "a/b", None, 7, b"deploy"]
)
def test_ad7_names_that_do_not_match_are_unreadable(name: object) -> None:
    with pytest.raises(UnreadableToolName) as caught:
        check_tool_name(name)
    if isinstance(name, str) and name.strip():
        assert name not in str(caught.value)  # the text is agent-chosen content


def test_ad8_c14_call_ids() -> None:
    assert call_id_for("c1") == "c1"
    assert call_id_for("call_Abc.9:x-y") == "call_Abc.9:x-y"
    assert call_id_for("call 1/α") == "h-3ad396cb1e7a9deefd1cf4085e94b6ec"
    assert re.fullmatch(r"h-[0-9a-f]{32}", call_id_for("x" * 129))
    assert re.fullmatch(r"h-[0-9a-f]{32}", call_id_for(""))
    made = call_id_for(None)
    assert re.fullmatch(r"c-[0-9a-f]{32}", made)
    assert call_id_for(None) != made


# --- AD-14 to AD-16 -----------------------------------------------------------------------------


def test_ad16_tool_set_hash_vectors() -> None:
    two = freeze_manifest(
        [ManifestEntry(name="read_file", schema=None), ManifestEntry("deploy", DEPLOY_SCHEMA)]
    )
    assert two.tool_set_hash == "b2f104001c4b5893c221ebbd981f391f7fd7ada8cf7476ab73aa9cf082f9022c"
    assert [e.name for e in two.tools] == ["deploy", "read_file"]  # sorted by name
    three = freeze_manifest(
        [
            ManifestEntry("drop_db", None),
            ManifestEntry("read_file", None),
            ManifestEntry("deploy", DEPLOY_SCHEMA),
        ]
    )
    assert three.tool_set_hash == "5fe874efe4efa96d3a5ac6feb65334b09a0b08ea6bb9286893d3860f6fa45b3d"
    assert three.ledger_tools() == [
        {"name": "deploy", "schema": DEPLOY_SCHEMA},
        {"name": "drop_db", "schema": None},
        {"name": "read_file", "schema": None},
    ]
    assert three.schema_of("deploy") == DEPLOY_SCHEMA and three.schema_of("drop_db") is None
    assert "deploy" in three and "send_mail" not in three


def test_ad14_schema_hash_is_the_digest_of_the_canonical_schema() -> None:
    a = schema_hash({"type": "object", "properties": {"env": {"type": "string"}}})
    b = schema_hash({"properties": {"env": {"type": "string"}}, "type": "object"})
    assert a == b and re.fullmatch(r"[0-9a-f]{64}", a)
    assert schema_hash({}) == EMPTY
    assert a != schema_hash({"type": "object", "properties": {"env": {"type": "integer"}}})


def test_ad15_c16_refused_manifests() -> None:
    with pytest.raises(ManifestError, match="deploy"):
        freeze_manifest([ManifestEntry("deploy", None), ManifestEntry("deploy", DEPLOY_SCHEMA)])
    with pytest.raises(ManifestError, match="AD-7"):
        freeze_manifest([ManifestEntry("bad name!", None)])
    with pytest.raises(ManifestError, match="256"):
        freeze_manifest([ManifestEntry(f"t{i}", None) for i in range(257)])
    with pytest.raises(ManifestError, match="deploy"):
        freeze_manifest([ManifestEntry("deploy", "not-a-hash")])
    assert len(freeze_manifest([ManifestEntry(f"t{i}", None) for i in range(256)]).tools) == 256
    assert isinstance(ManifestError("x"), ValueError)
    assert freeze_manifest([]).tools == ()


# --- section 9: codes and texts -----------------------------------------------------------------

SOFT = {
    "denied_by_rule",
    "ask_pending",
    "rejected_by_person",
    "approval_timeout",
    "approval_expired",
    "unreadable_arguments",
    "unreadable_tool_name",
    "unknown_delegate",
    "path_outside_workspace",
}
STOP = {
    "tool_not_frozen",
    "tool_set_changed",
    "tool_replaced",
    "arguments_changed",
    "org_halted",
    "role_paused",
    "run_stopped",
    "guarded_run_stopped",
    "no_budget",
    "budget_stop",
    "denial_limit",
    "ledger_unavailable",
    "server_unavailable",
    "adapter_error",
    "credential_rejected",
    "role_mismatch",
    "model_mismatch",
    "tool_token_invalid",
    "tool_calls_unanswered",
    "tool_message_orphan",
}
NEITHER = {"allow", "unknown_action_kind", "stopped", "shell_text"}


def test_ad41_every_reason_code_is_soft_or_stop_as_the_table_says() -> None:
    assert {c.value for c in ReasonCode} == SOFT | STOP | NEITHER
    for code in ReasonCode:
        assert is_soft_denial(code) == (code.value in SOFT), code
        assert is_stop(code) == (code.value in STOP), code


def test_lg1_lg3_the_three_task_refused_codes_of_the_langgraph_tool_spec_are_stops() -> None:
    """#971 section 9b (AD-77): three `task.refused` codes are added to the closed list
    of section 9.1, each a stop under AD-41 and never a soft denial (nothing is counted)."""
    added = {
        ReasonCode.TOOL_TOKEN_INVALID: "tool_token_invalid",
        ReasonCode.TOOL_CALLS_UNANSWERED: "tool_calls_unanswered",
        ReasonCode.TOOL_MESSAGE_ORPHAN: "tool_message_orphan",
    }
    for code, value in added.items():
        assert code.value == value
        assert is_stop(code) and not is_soft_denial(code)


def test_ad38_fixed_texts() -> None:
    assert deny_text(ReasonCode.DENIED_BY_RULE) == "Blocked by the team's rules: denied_by_rule."
    assert deny_text(ReasonCode.STOPPED) == "Blocked by the team's rules: stopped."
    assert ask_text("appr-0123456789abcdef") == (
        "Not done: this action needs a person's approval (approval appr-0123456789abcdef)."
    )
    with pytest.raises(ValueError):
        deny_text(ReasonCode.ALLOW)
    with pytest.raises(ValueError):
        deny_text(ReasonCode.SHELL_TEXT)  # never in a text the model sees (9.1)


# --- AD-42, AD-46, AD-69: option ranges ---------------------------------------------------------


def test_ad42_c37_the_denial_limit_can_be_lowered_never_raised_or_switched_off() -> None:
    assert DENIAL_LIMIT == 3
    assert [check_denial_limit(n) for n in (1, 2, 3)] == [1, 2, 3]
    for bad in (0, 4, -1, 100, None, True, 2.0, "3"):
        with pytest.raises(ValueError):
            check_denial_limit(bad)


def test_ad46_c33_the_deadline_is_honoured_inside_the_range_and_refused_outside() -> None:
    assert DEADLINE_WAIT_SECONDS == 600 and DEADLINE_DURABLE_SECONDS == 86400
    assert [check_deadline_seconds(n) for n in (1, 600, 604800)] == [1, 600, 604800]
    for bad in (0, 604801, -5, None, True, 1.5, "600"):
        with pytest.raises(ValueError):
            check_deadline_seconds(bad)


def test_manifest_error_never_carries_the_bad_name() -> None:
    secret = "agent-chosen-text-xyz!"
    with pytest.raises(ManifestError) as err:
        freeze_manifest([ManifestEntry("ok", None), ManifestEntry(secret, None)])
    assert "agent-chosen" not in str(err.value)
    assert "entry 1" in str(err.value) and "AD-7" in str(err.value)


# --- C-3 (#1119): the network-capable declaration in the manifest --------------------------------

NETWORK_VECTOR = "a2025ffaabc84249e7a7b83bbdec2967e8138cf6970fb31130e586db36ce9ff6"
UNDECLARED_VECTOR = "d7dd2b07eaf3fc5073510bf75a71c313caeac86de81eb96cd65c6b7e6e7ba969"


def _with_fetch(network: bool) -> list[ManifestEntry]:
    return [
        ManifestEntry("read_file", None),
        ManifestEntry("fetch_page", None, network=network),
        ManifestEntry("deploy", DEPLOY_SCHEMA),
    ]


def test_ad14_c3_a_manifest_entry_is_not_network_capable_unless_declared() -> None:
    assert ManifestEntry("deploy", DEPLOY_SCHEMA).network is False
    frozen = freeze_manifest(_with_fetch(network=False))
    assert [frozen.network_capable(n) for n in ("deploy", "fetch_page", "read_file")] == [
        False,
        False,
        False,
    ]


def test_ad14_c3_only_a_declared_frozen_tool_is_network_capable() -> None:
    frozen = freeze_manifest(_with_fetch(network=True))
    assert frozen.network_capable("fetch_page") is True
    assert frozen.network_capable("deploy") is False
    assert frozen.network_capable("read_file") is False
    # A name outside the frozen set is never network-capable (default deny), whatever its type.
    for outside in ("send_mail", "FETCH_PAGE", "", None, 1, ["fetch_page"]):
        assert frozen.network_capable(outside) is False
    assert [e.network for e in frozen.tools] == [False, True, False]  # kept through the sort


def test_ad16_c3_the_declaration_is_part_of_tool_set_hash() -> None:
    declared = freeze_manifest(_with_fetch(network=True))
    undeclared = freeze_manifest(_with_fetch(network=False))
    assert declared.tool_set_hash == NETWORK_VECTOR
    assert undeclared.tool_set_hash == UNDECLARED_VECTOR
    assert declared.tool_set_hash != undeclared.tool_set_hash
    # A set without any declaration hashes exactly as before the field existed (AD-16 vectors).
    two = freeze_manifest(
        [ManifestEntry("read_file", None, network=False), ManifestEntry("deploy", DEPLOY_SCHEMA)]
    )
    assert two.tool_set_hash == "b2f104001c4b5893c221ebbd981f391f7fd7ada8cf7476ab73aa9cf082f9022c"


def test_ad29_c3_the_freeze_record_shows_the_declaration() -> None:
    assert freeze_manifest(_with_fetch(network=True)).ledger_tools() == [
        {"name": "deploy", "schema": DEPLOY_SCHEMA},
        {"name": "fetch_page", "network": True, "schema": None},
        {"name": "read_file", "schema": None},
    ]
    assert freeze_manifest(_with_fetch(network=False)).ledger_tools() == [
        {"name": "deploy", "schema": DEPLOY_SCHEMA},
        {"name": "fetch_page", "schema": None},
        {"name": "read_file", "schema": None},
    ]


@pytest.mark.parametrize("flag", [1, 0, "true", "", None, [True]])
def test_ad15_c3_a_network_flag_that_is_not_a_boolean_refuses_the_manifest(flag: object) -> None:
    with pytest.raises(ManifestError) as caught:
        freeze_manifest([ManifestEntry("fetch_page", None, network=flag)])  # type: ignore[arg-type]
    assert str(caught.value) == "tool 'fetch_page': the network flag is not true or false (AD-14)"
