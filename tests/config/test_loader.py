"""Decision D1: YAML via a hardened safe loader (extra `nuramesh[yaml]`), JSON from the stdlib."""

from decimal import Decimal
from pathlib import Path

import pytest

from nuramesh.config import ConfigError, load_config_file, parse_json, parse_yaml
from nuramesh.config.loader import MAX_BYTES
from nuramesh.core.org import OrgGraph

pytest.importorskip("yaml")

ORG_YAML = """
id: acme
name: Acme
roles:
  - {id: chief-of-staff, name: Chief of Staff, kind: chief_of_staff}
  - {id: developer, name: Developer}
agents:
  - {id: cos, name: Casey, role: chief-of-staff}
  - {id: dev-1, name: Developer, role: developer, reports_to: cos}
"""


def test_yaml_org_builds_an_org_graph() -> None:
    org = OrgGraph.build(parse_yaml(ORG_YAML))
    assert org.chief_of_staff.id == "cos"


def test_yaml_and_json_give_the_same_data() -> None:
    import json

    data = parse_yaml(ORG_YAML)
    assert parse_json(json.dumps(data)) == data


@pytest.mark.parametrize(
    "payload",
    [
        "!!python/object/apply:os.system ['echo pwned']",
        "x: !!python/object/apply:os.system ['echo pwned']",
        "x: !!python/name:os.system",
        "x: !!python/object/new:subprocess.Popen [['ls']]",
    ],
)
def test_unsafe_tags_are_rejected(payload: str) -> None:
    with pytest.raises(ConfigError):
        parse_yaml(payload)


def test_aliases_and_anchors_are_rejected() -> None:
    bomb = "a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\n"
    with pytest.raises(ConfigError, match="anchors are not allowed"):
        parse_yaml(bomb)
    with pytest.raises(ConfigError):
        parse_yaml("a: *missing\n")


def test_duplicate_keys_are_rejected_in_both_formats() -> None:
    with pytest.raises(ConfigError, match="duplicate key 'max_attempts'"):
        parse_yaml("max_attempts: 3\nmax_attempts: 50\n")
    with pytest.raises(ConfigError, match="duplicate key 'max_attempts'"):
        parse_json('{"max_attempts": 3, "max_attempts": 50}')
    with pytest.raises(ConfigError, match="duplicate key 'x'"):
        parse_yaml("outer:\n  x: 1\n  x: 2\n")


def test_decimals_never_become_floats() -> None:
    data = parse_yaml("limit: 12.50\nrate: 1e-7\nn: 3\nunderscored: 1_000.5\n")
    assert data == {
        "limit": Decimal("12.50"),
        "rate": Decimal("1e-7"),
        "n": 3,
        "underscored": "1_000.5",  # YAML 1.2 style: underscores aren't numbers
    }
    assert parse_json('{"limit": 12.50}') == {"limit": Decimal("12.50")}


@pytest.mark.parametrize("text", ["x: .nan", "x: .inf", "x: -.Inf"])
def test_yaml_nan_and_infinity_spellings_stay_text(text: str) -> None:
    # Never numbers; numeric model fields refuse the text. `!!float nan` is refused outright.
    assert isinstance(parse_yaml(text)["x"], str)


@pytest.mark.parametrize("bad", ['{"x": NaN}', '{"x": Infinity}'])
def test_json_nan_and_infinity_are_rejected(bad: str) -> None:
    with pytest.raises(ConfigError):
        parse_json(bad)


def test_dates_stay_text() -> None:
    assert parse_yaml("effective_from: 2026-10-20\nat: 2026-10-20T12:00:00Z\n") == {
        "effective_from": "2026-10-20",
        "at": "2026-10-20T12:00:00Z",
    }


@pytest.mark.parametrize("text", ["", "- a\n- b\n", "just a string\n"])
def test_top_level_must_be_a_mapping(text: str) -> None:
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        parse_yaml(text)


def test_errors_name_the_source_and_line() -> None:
    with pytest.raises(ConfigError, match=r"org\.yaml: .*line 3"):
        parse_yaml("a: 1\nb: 2\na: 3\n", source="org.yaml")
    with pytest.raises(ConfigError, match=r"org\.json: line 1, column"):
        parse_json("{oops}", source="org.json")


def test_size_limit() -> None:
    with pytest.raises(ConfigError, match="limit"):
        parse_yaml("x: " + "a" * MAX_BYTES)


def test_load_config_file_by_extension(tmp_path: Path) -> None:
    (tmp_path / "org.yaml").write_text(ORG_YAML, encoding="utf-8")
    (tmp_path / "org.json").write_text('{"id": "acme"}', encoding="utf-8")
    (tmp_path / "org.toml").write_text("id = 'acme'", encoding="utf-8")
    assert load_config_file(tmp_path / "org.yaml")["id"] == "acme"
    assert load_config_file(tmp_path / "org.json") == {"id": "acme"}
    with pytest.raises(ConfigError, match="use a .yaml"):
        load_config_file(tmp_path / "org.toml")
    with pytest.raises(ConfigError):
        load_config_file(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("country: NO", "NO"),
        ("x: yes", "yes"),
        ("x: on", "on"),
        ("x: 017", 17),
        ("x: 0x1F", "0x1F"),
        ("x: 1:30", "1:30"),
        ("x: True", True),
        ("x: FALSE", False),
        ("x: ~", None),
        ("x:", None),
        ("x: -5", -5),
        ("x: +.5", Decimal("+.5")),
        ("x: .inf", ".inf"),
    ],
)
def test_yaml_12_style_plain_scalars(text: str, value: object) -> None:
    assert parse_yaml(text) == {"x" if not text.startswith("country") else "country": value}


@pytest.mark.parametrize(
    "bad",
    [
        "x: !!binary aGVsbG8=",
        "x: !!set {a, b}",
        "x: !!omap [{a: 1}, {a: 2}]",
        "x: !!pairs [{a: 1}]",
        "x: !custom thing",
        "x: !!float nan",
        "x: !!float abc",
        "x: !!int abc",
        "x: !!bool maybe",
        "x: !!null foo",
        "? [a, b]\n: 1",
        "? {a: 1}\n: 1",
        "1: a",
        "1.5: a",
        "true: a",
        "a: 1\n---\nb: 2",
        "x: 1e999",
    ],
)
def test_yaml_refusals_are_config_errors(bad: str) -> None:
    with pytest.raises(ConfigError):
        parse_yaml(bad)


@pytest.mark.parametrize("parse", [parse_yaml, parse_json])
def test_deep_nesting_is_refused_cleanly(parse: object) -> None:
    deep = "[" * 70 + "]" * 70
    text = '{"x": ' + deep + "}" if parse is parse_json else "x: " + deep
    with pytest.raises(ConfigError, match="nested"):
        parse(text, source="t")  # type: ignore[operator]
    very_deep = "[" * 5000 + "]" * 5000
    text = '{"x": ' + very_deep + "}" if parse is parse_json else "x: " + very_deep
    with pytest.raises(ConfigError):
        parse(text, source="t")  # type: ignore[operator]


@pytest.mark.parametrize("parse", [parse_yaml, parse_json])
def test_huge_integers_are_config_errors(parse: object) -> None:
    digits = "9" * 5000
    text = '{"x": ' + digits + "}" if parse is parse_json else "x: " + digits
    with pytest.raises(ConfigError):
        parse(text)  # type: ignore[operator]


def test_lone_surrogates_are_refused() -> None:
    with pytest.raises(ConfigError, match="surrogate"):
        parse_json('{"x": "\\ud800"}')


def test_byte_order_mark_is_accepted_for_both_formats(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('\ufeff{"x": 1}', encoding="utf-8")
    (tmp_path / "a.yaml").write_text("\ufeffx: 1\n", encoding="utf-8")
    assert (
        load_config_file(tmp_path / "a.json") == load_config_file(tmp_path / "a.yaml") == {"x": 1}
    )
