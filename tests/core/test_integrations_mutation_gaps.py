"""Mutation pins for core.integrations (#1310): length limits, personal packs, error text."""

import hashlib
from typing import Any

import pytest
from pydantic import ValidationError

from nuramesh.core.canonical import canonical_json
from nuramesh.core.integrations import (
    ACK_KIND,
    ENGINE_BUILD_ACTOR,
    Effect,
    IntegrationEntry,
    IntegrationError,
    Pack,
    PackTool,
    acknowledgement_payload,
    definition_hash_of,
    definition_set_hash,
    duplicate_servers,
    expand_entry,
    parse_pack,
    validate_entry,
)

H = "a" * 64
HB = "b" * 64


def _tool(name: str = "read_x", **extra: Any) -> dict[str, Any]:
    return {"tool": name, "kind": "read", "allowed_args": ["a"], **extra}


def _pack(data_kind: str = "code", **extra: Any) -> Pack:
    data: dict[str, Any] = {
        "name": "demo",
        "pack_version": 3,
        "data_kind": data_kind,
        "tools": [_tool("t1"), _tool("t2")],
    }
    return parse_pack({**data, **extra})


def _entry(**extra: Any) -> dict[str, Any]:
    return {
        "pack": "demo",
        "pack_version": 3,
        "server": "srv",
        "definition_hashes": {"t1": H, "t2": HB},
        **extra,
    }


def _refusal(raw: Any, pack: Pack | None) -> str:
    with pytest.raises(IntegrationError) as caught:
        validate_entry(raw, pack, 2)
    return str(caught.value)


# --- constants -------------------------------------------------------------------------------


def test_constants() -> None:
    assert ENGINE_BUILD_ACTOR == "system:engine-build"
    assert ACK_KIND == "personal_data_acknowledged"


# --- PackTool / Effect limits ----------------------------------------------------------------


def test_effect_arg_length_limits() -> None:
    assert Effect(kind="merge", arg="x" * 128).arg == "x" * 128
    with pytest.raises(ValueError):
        Effect(kind="merge", arg="x" * 129)
    with pytest.raises(ValueError):
        Effect(kind="merge", arg="")


def test_tool_name_pattern_and_length() -> None:
    parse_pack({**_pack().model_dump(), "tools": [_tool("A_b.c-9")]})
    parse_pack({**_pack().model_dump(), "tools": [_tool("x" * 128)]})
    for bad in ("x" * 129, "", "has space", "a/b"):
        with pytest.raises(IntegrationError):
            parse_pack({**_pack().model_dump(), "tools": [_tool(bad)]})


def test_allowed_args_limit_is_64() -> None:
    names = [f"a{i}" for i in range(64)]
    assert len(PackTool.model_validate(_tool(allowed_args=names)).allowed_args) == 64
    with pytest.raises(ValueError):
        PackTool.model_validate(_tool(allowed_args=[*names, "a64"]))


def test_effects_limit_is_8_and_paid_is_false() -> None:
    args = [f"a{i}" for i in range(9)]

    def write(count: int) -> dict[str, Any]:
        return {
            "tool": "w",
            "kind": "write",
            "allowed_args": args,
            "effects": [{"kind": "merge", "arg": a} for a in args[:count]],
        }

    tool = PackTool.model_validate(write(8))
    assert len(tool.effects) == 8
    assert tool.paid is False
    with pytest.raises(ValueError):
        PackTool.model_validate(write(9))
    with pytest.raises(ValueError):
        PackTool.model_validate({**write(1), "paid": True})


def test_effects_default_is_empty() -> None:
    assert PackTool.model_validate(_tool()).effects == ()


def test_tool_shape_rules() -> None:
    effect = {"kind": "merge", "arg": "a"}
    with pytest.raises(ValueError, match="repeats a name"):
        PackTool.model_validate(_tool(allowed_args=["a", "a"]))
    with pytest.raises(ValueError, match="read tool has no effects"):
        PackTool.model_validate(_tool(effects=[effect]))
    with pytest.raises(ValueError, match="needs at least one effect"):
        PackTool.model_validate(_tool(kind="write"))
    ok = PackTool.model_validate(_tool(kind="write", effects=[effect]))
    assert ok.kind == "write"
    with pytest.raises(ValueError, match="distinct member"):
        PackTool.model_validate(_tool(kind="write", effects=[effect, effect]))
    with pytest.raises(ValueError, match="distinct member"):
        PackTool.model_validate(_tool(kind="write", effects=[{"kind": "merge", "arg": "zz"}]))


# --- Pack ------------------------------------------------------------------------------------


def test_pack_version_minimum_and_tool_count_limits() -> None:
    assert _pack(pack_version=1).pack_version == 1
    with pytest.raises(IntegrationError):
        _pack(pack_version=0)
    many = [_tool(f"t{i}") for i in range(64)]
    assert len(_pack(tools=many).tools) == 64
    with pytest.raises(IntegrationError):
        _pack(tools=[*many, _tool("t64")])
    with pytest.raises(IntegrationError):
        _pack(tools=[])


def test_data_kind_only_code_or_personal() -> None:
    assert _pack("personal").data_kind == "personal"
    with pytest.raises(IntegrationError):
        _pack("secret")


def test_pack_name_is_a_slug_of_at_most_63() -> None:
    assert _pack(name="a" + "b" * 62).name == "a" + "b" * 62
    for bad in ("a" + "b" * 63, "Demo", "1abc", "", "a_b"):
        with pytest.raises(IntegrationError):
            _pack(name=bad)


def test_repeated_tool_name_is_refused() -> None:
    with pytest.raises(IntegrationError) as caught:
        _pack(tools=[_tool("t1"), _tool("t1")])
    assert str(caught.value) == "pack 'demo' is invalid at: (entry)"


def test_parse_pack_message_names_pack_and_paths_only() -> None:
    with pytest.raises(IntegrationError) as caught:
        parse_pack({"name": "demo", "pack_version": 0, "data_kind": "code", "tools": [_tool()]})
    assert str(caught.value) == "pack 'demo' is invalid at: pack_version"
    with pytest.raises(IntegrationError) as unnamed:
        parse_pack({"name": 5})
    assert str(unnamed.value).startswith("pack '?' is invalid at: ")
    with pytest.raises(IntegrationError) as two:
        parse_pack({"name": "demo", "pack_version": 0, "data_kind": "x", "tools": [_tool()]})
    assert str(two.value) == "pack 'demo' is invalid at: data_kind, pack_version"


# --- hashes ----------------------------------------------------------------------------------


def test_definition_hashes_are_sha256_of_canonical_json() -> None:
    obj: dict[str, Any] = {"b": 1, "a": ["x"]}
    assert definition_hash_of(obj) == hashlib.sha256(canonical_json(obj)).hexdigest()
    hashes = {"t2": HB, "t1": H}
    assert (
        definition_set_hash(hashes)
        == hashlib.sha256(canonical_json({"t1": H, "t2": HB})).hexdigest()
    )
    assert definition_set_hash(hashes) == definition_set_hash({"t1": H, "t2": HB})


# --- validate_entry --------------------------------------------------------------------------


def test_valid_entry_returns_normalised_entry() -> None:
    entry = validate_entry(_entry(), _pack(), 0)
    assert entry == IntegrationEntry(
        pack="demo",
        pack_version=3,
        server="srv",
        definition_hashes={"t1": H, "t2": HB},
        acknowledge_personal_data=None,
    )


def test_non_mapping_and_unknown_key() -> None:
    assert _refusal(["x"], _pack()) == "integrations[2] must be a mapping"
    assert _refusal(_entry(extra=1), _pack()) == "integrations[2]: key 'extra' is not allowed"


def test_unknown_pack_names_the_pack_or_a_question_mark() -> None:
    assert _refusal(_entry(), None) == "integrations[2]: unknown pack 'demo'"
    assert _refusal({"pack": 7}, None) == "integrations[2]: unknown pack '?'"


def test_acknowledgement_only_for_personal_packs() -> None:
    assert _refusal(_entry(acknowledge_personal_data=True), _pack()) == (
        "integrations[2]: acknowledge_personal_data is only for personal packs, not 'demo'"
    )
    # even a false or null value is refused for a code pack: the key itself is the problem
    assert "only for personal packs" in _refusal(_entry(acknowledge_personal_data=False), _pack())


def test_personal_pack_needs_literal_true() -> None:
    personal = _pack("personal")
    expected = (
        "integrations[2]: pack 'demo' holds personal data and needs acknowledge_personal_data: true"
    )
    assert _refusal(_entry(), personal) == expected
    for wrong in (False, 1, "true", None):
        assert _refusal(_entry(acknowledge_personal_data=wrong), personal) == expected
    entry = validate_entry(_entry(acknowledge_personal_data=True), personal, 0)
    assert entry.acknowledge_personal_data is True


def test_pack_version_must_be_the_exact_int() -> None:
    def message(shown: str) -> str:
        return (
            f"integrations[2]: pack 'demo' is at version 3 but the entry says {shown}; "
            "re-pin the hashes and set pack_version: 3"
        )

    assert _refusal(_entry(pack_version=2), _pack()) == message("2")
    assert _refusal(_entry(pack_version="3"), _pack()) == message("?")
    assert _refusal(_entry(pack_version=True), _pack()) == message("?")
    raw = _entry()
    del raw["pack_version"]
    assert _refusal(raw, _pack()) == message("?")


def test_server_slug_boundaries() -> None:
    assert validate_entry(_entry(server="a" + "b" * 62), _pack(), 0).server == "a" + "b" * 62
    for bad in ("a" + "b" * 63, "Srv", "9srv", "", 5, None):
        assert _refusal(_entry(server=bad), _pack()) == "integrations[2]: server is not a slug"


def test_definition_hashes_must_be_a_mapping() -> None:
    expected = "integrations[2]: pack 'demo' needs definition_hashes"
    assert _refusal(_entry(definition_hashes=[H]), _pack()) == expected
    raw = _entry()
    del raw["definition_hashes"]
    assert _refusal(raw, _pack()) == expected


def test_missing_and_extra_hash_messages() -> None:
    assert _refusal(_entry(definition_hashes={"t1": H}), _pack()) == (
        "integrations[2]: pack 'demo' has no definition hash for tool 't2'"
    )
    assert _refusal(_entry(definition_hashes={"t1": H, "t2": HB, "zz": H}), _pack()) == (
        "integrations[2]: pack 'demo' has no tool 'zz' (extra definition hash)"
    )


def test_hash_must_be_64_lowercase_hex() -> None:
    message = "integrations[2]: pack 'demo' tool 't2': the hash is not 64 lowercase hex"
    for bad in (HB[:-1], HB + "b", "B" * 64, "g" * 64, 5, None):
        assert _refusal(_entry(definition_hashes={"t1": H, "t2": bad}), _pack()) == message
    ok = validate_entry(_entry(definition_hashes={"t1": "ab" * 32, "t2": HB}), _pack(), 0)
    assert ok.definition_hashes["t1"] == "ab" * 32


def test_personal_entry_is_marked_acknowledged_and_hashes_are_copied() -> None:
    raw = _entry(acknowledge_personal_data=True)
    entry = validate_entry(raw, _pack("personal"), 0)
    assert entry.acknowledge_personal_data is True
    assert entry.definition_hashes == {"t1": H, "t2": HB}
    assert entry.definition_hashes is not raw["definition_hashes"]


# --- expansion, acknowledgement, duplicates --------------------------------------------------


def test_expand_entry_shapes() -> None:
    pack = parse_pack(
        {
            "name": "demo",
            "pack_version": 3,
            "data_kind": "code",
            "tools": [
                _tool("r"),
                {
                    "tool": "w",
                    "kind": "write",
                    "allowed_args": ["a", "b"],
                    "effects": [{"kind": "merge", "arg": "b"}],
                },
            ],
        }
    )
    entry = validate_entry(_entry(definition_hashes={"r": H, "w": HB}), pack, 0)
    server, tools = expand_entry(entry, pack)
    assert server == {"id": "srv", "transport": "host"}
    assert tools == [
        {
            "server": "srv",
            "tool": "r",
            "allowed_args": ["a"],
            "definition_hash": H,
            "effects": [],
            "paid": False,
        },
        {
            "server": "srv",
            "tool": "w",
            "allowed_args": ["a", "b"],
            "definition_hash": HB,
            "effects": [{"kind": "merge", "arg": "b"}],
            "paid": False,
        },
    ]
    assert isinstance(tools[0]["allowed_args"], list)


def test_acknowledgement_payload_is_exactly_seven_keys() -> None:
    pack = _pack("personal")
    entry = validate_entry(_entry(acknowledge_personal_data=True), pack, 0)
    assert acknowledgement_payload(entry, pack) == {
        "kind": "personal_data_acknowledged",
        "pack": "demo",
        "server": "srv",
        "pack_version": 3,
        "data_kind": "personal",
        "definition_set_hash": definition_set_hash({"t1": H, "t2": HB}),
        "actor": "system:engine-build",
    }


def _entries(*servers: str) -> list[IntegrationEntry]:
    return [
        IntegrationEntry(pack="demo", pack_version=3, server=s, definition_hashes={})
        for s in servers
    ]


def test_duplicate_servers_lists_each_once_in_first_repeat_order() -> None:
    assert duplicate_servers(_entries("a", "b", "c")) == []
    assert duplicate_servers(_entries("a", "a")) == ["a"]
    assert duplicate_servers(_entries("a", "b", "b", "a", "b", "a", "c")) == ["b", "a"]
    assert duplicate_servers([]) == []


def test_models_are_frozen() -> None:
    pack = _pack()
    entry = validate_entry(_entry(), pack, 0)
    for model, field in (
        (Effect(kind="merge", arg="a"), "arg"),
        (pack.tools[0], "tool"),
        (pack, "name"),
        (entry, "server"),
    ):
        with pytest.raises(ValidationError):
            setattr(model, field, "other")  # noqa: B010


def test_slug_allows_digits_and_hyphens_and_one_character() -> None:
    assert _pack(name="a").name == "a"
    assert _pack(name="a1b2-c9").name == "a1b2-c9"
    assert validate_entry(_entry(server="a"), _pack(), 0).server == "a"
    assert validate_entry(_entry(server="s0-9z"), _pack(), 0).server == "s0-9z"


def test_direct_validation_messages_name_the_rule() -> None:
    with pytest.raises(ValidationError, match="tool name does not match the tool pattern"):
        PackTool.model_validate(_tool("has space"))
    base = _pack().model_dump()
    with pytest.raises(ValidationError, match="pack name is not a slug"):
        Pack.model_validate({**base, "name": "Bad"})
    with pytest.raises(ValidationError, match="a tool name is repeated"):
        Pack.model_validate({**base, "tools": [_tool("t1"), _tool("t1")]})
