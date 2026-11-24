"""Mutation pins for the config loader (#1329): exact limits, messages, decimals and refusals."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from nuramesh.config import ConfigError, load_config_file, parse_json, parse_yaml
from nuramesh.config.loader import MAX_BYTES, MAX_DECIMAL_EXPONENT, MAX_DEPTH

pytest.importorskip("yaml")


def test_limits_are_exact() -> None:
    assert MAX_BYTES == 2 * 1024 * 1024
    assert MAX_DEPTH == 64
    assert MAX_DECIMAL_EXPONENT == 100


def test_size_limit_is_inclusive_and_counts_bytes() -> None:
    head = '{"a":"'
    tail = '"}'
    ok = head + "x" * (MAX_BYTES - len(head) - len(tail)) + tail
    assert len(ok.encode()) == MAX_BYTES
    assert parse_json(ok)["a"].startswith("x")
    with pytest.raises(ConfigError) as exc:
        parse_json(ok + " ", source="big.json")
    assert str(exc.value) == f"big.json: {MAX_BYTES + 1} bytes is over the {MAX_BYTES}-byte limit"
    # two-byte characters count as two bytes, not one
    wide = '{"a":"' + "é" * (MAX_BYTES // 2) + '"}'
    with pytest.raises(ConfigError, match="bytes is over"):
        parse_json(wide)
    with pytest.raises(ConfigError, match="bytes is over"):
        parse_yaml("a: " + "x" * MAX_BYTES, source="big.yaml")


def _nested(levels: int) -> str:
    return '{"a":' * levels + "1" + "}" * levels


def test_depth_limit_json_boundary_and_message() -> None:
    assert parse_json(_nested(MAX_DEPTH))  # 64 mapping levels below the top is allowed
    with pytest.raises(ConfigError) as exc:
        parse_json(_nested(MAX_DEPTH + 1), source="d.json")
    assert str(exc.value).startswith("d.json: $")
    assert f"nested more than {MAX_DEPTH} levels" in str(exc.value)


def test_depth_limit_lists_count_too() -> None:
    ok = "[" * (MAX_DEPTH - 1) + "1" + "]" * (MAX_DEPTH - 1)
    assert parse_json('{"a":' + ok + "}") is not None
    bad = "[" * MAX_DEPTH + "1" + "]" * MAX_DEPTH
    with pytest.raises(ConfigError, match="nested more than"):
        parse_json('{"a":' + bad + "}")


def test_error_path_names_the_key_and_list_index() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_json('{"a":{"b":[1,{"c":1e999999}]}}', source="p.json")
    assert "$.a.b[1].c: exponent beyond" in str(exc.value)


def test_json_non_text_key_is_impossible_but_lone_surrogate_key_and_value_are_refused() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_json('{"a\\ud800":1}', source="s.json")
    assert str(exc.value) == "s.json: $: text contains a lone surrogate"
    with pytest.raises(ConfigError) as exc:
        parse_json('{"k":"\\ud800"}', source="s.json")
    assert str(exc.value) == "s.json: $.k: text contains a lone surrogate"
    assert parse_json('{"k":"\\ud83d\\ude00"}') == {"k": "\U0001f600"}


def test_decimal_exponent_boundary_json() -> None:
    assert parse_json('{"a":1e-100}')["a"] == Decimal("1e-100")
    assert parse_json('{"a":1e100}')["a"] == Decimal("1e100")
    with pytest.raises(ConfigError, match=r"exponent beyond ±100"):
        parse_json('{"a":1e-101}')
    with pytest.raises(ConfigError, match=r"exponent beyond ±100"):
        parse_json('{"a":1e101}')


def test_floats_are_decimals_and_ints_stay_ints_and_bools_are_kept() -> None:
    data = parse_json('{"f":1.50,"i":3,"t":true,"f2":false,"n":null,"l":[],"s":"x"}')
    assert data["f"] == Decimal("1.50") and str(data["f"]) == "1.50"
    assert type(data["i"]) is int and data["i"] == 3
    assert data["t"] is True and data["f2"] is False and data["n"] is None
    assert data["l"] == [] and data["s"] == "x"


@pytest.mark.parametrize("const", ["NaN", "Infinity", "-Infinity"])
def test_json_constants_are_refused_with_name(const: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_json('{"a":' + const + "}", source="c.json")
    assert str(exc.value) == f"c.json: '{const}' is not allowed"


def test_json_duplicate_key_message() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_json('{"a":1,"a":2}', source="d.json")
    assert str(exc.value) == "d.json: duplicate key 'a'"


def test_json_syntax_error_reports_line_and_column() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_json('{\n  "a": ,\n}', source="e.json")
    assert str(exc.value).startswith("e.json: line 2, column 8: ")


def test_json_deep_recursion_is_a_config_error() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_json("[" * 200000, source="r.json")
    assert str(exc.value) == "r.json: nested too deeply"


@pytest.mark.parametrize(
    ("text", "kind"), [("[1]", "list"), ("3", "int"), ('"x"', "str"), ("null", "empty")]
)
def test_top_level_must_be_a_mapping(text: str, kind: str) -> None:
    with pytest.raises(ConfigError) as exc:
        parse_json(text, source="t.json")
    assert str(exc.value) == f"t.json: the top level must be a mapping, not {kind}"


def test_default_sources() -> None:
    with pytest.raises(ConfigError, match=r"^<json>: "):
        parse_json("[")
    with pytest.raises(ConfigError, match=r"^<yaml>: "):
        parse_yaml("- a")
    with pytest.raises(ConfigError, match=r"^<yaml>: "):
        parse_yaml("a: &x 1")


def test_yaml_error_messages_carry_the_source_and_line() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_yaml("a: 1\nb: *x\n", source="y.yaml")
    assert str(exc.value) == "y.yaml: line 2: aliases are not allowed"
    with pytest.raises(ConfigError) as exc:
        parse_yaml("a: 1\nb: &x 2\n", source="y.yaml")
    assert str(exc.value) == "y.yaml: line 2: anchors are not allowed"
    with pytest.raises(ConfigError) as exc:
        parse_yaml("a: 1\nb: !!binary aGk=\n", source="y.yaml")
    assert str(exc.value) == "y.yaml: line 2: tag 'tag:yaml.org,2002:binary' is not allowed"
    with pytest.raises(ConfigError) as exc:
        parse_yaml("a: 1\n1: x\n", source="y.yaml")
    assert str(exc.value) == "y.yaml: line 2: keys must be text"
    with pytest.raises(ConfigError) as exc:
        parse_yaml("a: 1\n\nb: 1\na: 2\n", source="y.yaml")
    assert str(exc.value) == "y.yaml: line 4: duplicate key 'a'"


def test_yaml_allowed_core_tags_work() -> None:
    data = parse_yaml(
        "a: !!str 5\nb: !!int '7'\nc: !!float '1.5'\nd: !!bool 'true'\ne: !!null ''\n"
    )
    assert data == {"a": "5", "b": 7, "c": Decimal("1.5"), "d": True, "e": None}
    assert parse_yaml("a: !!map {b: 1}\nc: !!seq [1]\n") == {"a": {"b": 1}, "c": [1]}


def test_yaml_depth_limit_boundary() -> None:
    def nest(levels: int) -> str:
        return "".join("  " * i + "a:\n" for i in range(levels)) + "  " * levels + "v: 1\n"

    assert parse_yaml(nest(MAX_DEPTH - 2))
    with pytest.raises(ConfigError, match=f"nested more than {MAX_DEPTH} levels"):
        parse_yaml(nest(MAX_DEPTH + 2), source="n.yaml")
    flow = "a: " + "[" * MAX_DEPTH + "1" + "]" * MAX_DEPTH
    with pytest.raises(ConfigError, match=r"^<yaml>: line 1: nested more than 64 levels"):
        parse_yaml(flow)


def test_yaml_depth_counter_is_released_between_siblings() -> None:
    # many shallow siblings must not accumulate towards the limit
    text = "".join(f"k{i}: {{a: 1}}\n" for i in range(MAX_DEPTH * 3))
    assert len(parse_yaml(text)) == MAX_DEPTH * 3


def test_yaml_scalars_follow_the_strict_rules() -> None:
    data = parse_yaml(
        "a: TRUE\nb: False\nc: ~\nd: Null\ne:\nf: 017\ng: -5\nh: +5\ni: 1.5\nj: .5\nk: 1e-7\n"
        "l: 5.\nm: NO\nn: yes\no: on\np: 0x1F\nq: 1:30\nr: .nan\ns: 2020-01-01\nt: nULL\nu: tRUE\n"
    )
    assert data["a"] is True and data["b"] is False
    assert data["c"] is None and data["d"] is None and data["e"] is None
    assert data["f"] == 17 and type(data["f"]) is int
    assert data["g"] == -5 and type(data["g"]) is int
    assert data["h"] == 5 and type(data["h"]) is int
    assert data["i"] == Decimal("1.5") and data["j"] == Decimal("0.5")
    assert data["k"] == Decimal("1e-7") and data["l"] == Decimal("5")
    for key in "mnopqrstu":
        assert isinstance(data[key], str), key
    assert data["m"] == "NO" and data["r"] == ".nan" and data["s"] == "2020-01-01"
    assert data["t"] == "nULL" and data["u"] == "tRUE"


def test_yaml_explicit_tag_mismatches_name_the_text() -> None:
    cases = [
        ("a: !!int abc", "line 1: 'abc' isn't a decimal integer"),
        ("a: !!int 0x10", "line 1: '0x10' isn't a decimal integer"),
        ("a: !!null x", "line 1: 'x' isn't null"),
        ("a: !!bool yes", "line 1: 'yes' isn't true or false"),
        ("a: !!float abc", "line 1: 'abc' isn't a number"),
        ("a: !!float inf", "line 1: NaN and Infinity are not allowed"),
        ("a: !!float nan", "line 1: NaN and Infinity are not allowed"),
        ("a: 1\nb: !!float abc", "line 2: 'abc' isn't a number"),
    ]
    for text, tail in cases:
        with pytest.raises(ConfigError) as exc:
            parse_yaml(text, source="t.yaml")
        assert str(exc.value) == f"t.yaml: {tail}", text


def test_yaml_bool_case_is_lowered() -> None:
    assert parse_yaml("a: !!bool 'True'\nb: !!bool 'FALSE'") == {"a": True, "b": False}


def test_yaml_decimal_exponent_and_tree_checks() -> None:
    assert parse_yaml("a: 1e100")["a"] == Decimal("1e100")
    with pytest.raises(ConfigError, match=r"^t.yaml: \$: exponent beyond ±100$|\$.a: exponent"):
        parse_yaml("a: 1e101", source="t.yaml")
    with pytest.raises(ConfigError) as exc:
        parse_yaml('a: "\\ud800"', source="t.yaml")
    assert str(exc.value) == "t.yaml: $.a: text contains a lone surrogate"


def test_yaml_multiple_documents_and_syntax_errors_are_config_errors() -> None:
    with pytest.raises(ConfigError, match=r"^m.yaml: "):
        parse_yaml("a: 1\n---\nb: 2\n", source="m.yaml")
    with pytest.raises(ConfigError, match=r"^m.yaml: "):
        parse_yaml("a: [1,\n", source="m.yaml")


def test_yaml_empty_document_is_refused_as_empty() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_yaml("", source="e.yaml")
    assert str(exc.value) == "e.yaml: the top level must be a mapping, not empty"


def test_load_file_by_suffix_and_error_messages(tmp_path: Path) -> None:
    j = tmp_path / "a.JSON"
    j.write_text('{"a": 1.5}', encoding="utf-8")
    assert load_config_file(j) == {"a": Decimal("1.5")}
    assert load_config_file(str(j)) == {"a": Decimal("1.5")}
    for name in ("b.yaml", "c.yml", "d.YML"):
        f = tmp_path / name
        f.write_text("a: 1\n", encoding="utf-8")
        assert load_config_file(f) == {"a": 1}
    txt = tmp_path / "e.txt"
    txt.write_text("a: 1")
    with pytest.raises(ConfigError) as exc:
        load_config_file(txt)
    assert str(exc.value) == f"{txt}: use a .yaml, .yml or .json file"
    missing = tmp_path / "nope.json"
    with pytest.raises(ConfigError) as exc:
        load_config_file(missing)
    assert str(exc.value).startswith(f"{missing}: ")
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"a":"\xff"}')
    with pytest.raises(ConfigError) as exc:
        load_config_file(bad)
    assert str(exc.value).startswith(f"{bad}: ")


def test_load_file_strips_a_byte_order_mark_and_names_the_file_in_errors(tmp_path: Path) -> None:
    f = tmp_path / "bom.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"a": 1}).encode())
    assert load_config_file(f) == {"a": 1}
    y = tmp_path / "bom.yaml"
    y.write_bytes(b"\xef\xbb\xbfa: 1\n")
    assert load_config_file(y) == {"a": 1}
    g = tmp_path / "g.yaml"
    g.write_text("a: *x\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=f"^{g}: "):
        load_config_file(g)
    h = tmp_path / "h.json"
    h.write_text("[", encoding="utf-8")
    with pytest.raises(ConfigError, match=f"^{h}: "):
        load_config_file(h)


def test_a_literal_lone_surrogate_is_a_config_error_not_an_encoding_crash() -> None:
    with pytest.raises(ConfigError) as exc:
        parse_json('{"a": "\ud800"}', source="s.json")
    assert str(exc.value) == "s.json: $.a: text contains a lone surrogate"
    with pytest.raises(ConfigError) as exc:
        parse_yaml("a: x\ud800y", source="s.yaml")
    assert "s.yaml: " in str(exc.value)


def test_the_strict_loader_class_is_built_once() -> None:
    from nuramesh.config import loader

    assert loader._strict_loader() is loader._strict_loader()


def test_json_decimal_overflow_and_huge_integers_are_config_errors() -> None:
    with pytest.raises(ConfigError, match=r"^o.json: "):
        parse_json('{"a": 1e99999999999999999999}', source="o.json")
    with pytest.raises(ConfigError, match=r"^o.json: "):
        parse_json('{"a": 1' + "0" * 5000 + "}", source="o.json")
    with pytest.raises(ConfigError, match=r"^o.yaml: "):
        parse_yaml("a: 1" + "0" * 5000, source="o.yaml")
