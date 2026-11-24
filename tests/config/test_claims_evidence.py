"""Evidence for BASELINE 1.8: YAML is read with a safe loader (P0-10)."""

from pathlib import Path

import pytest

pytest.importorskip("yaml")

from nuramesh.config import ConfigError, parse_yaml  # noqa: E402


@pytest.mark.parametrize(
    "text",
    [
        "x: !!python/object/apply:os.system ['echo pwned']\n",
        "x: !!python/name:os.system\n",
        "x: !!python/object:builtins.object {}\n",
    ],
)
def test_python_object_tags_are_refused_and_nothing_runs(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_yaml(text)


def test_an_object_tag_executes_nothing(tmp_path: Path) -> None:
    """A payload that would create a marker directory under an unsafe loader leaves no marker."""
    marker = tmp_path / "ran"
    payload = f"x: !!python/object/apply:os.mkdir [{marker}]\n"
    with pytest.raises(ConfigError):
        parse_yaml(payload)
    assert not marker.exists()
    # Control: the same payload under yaml's unsafe loader does create it, so this test can fail.
    import yaml

    try:
        yaml.load(payload, Loader=yaml.UnsafeLoader)  # noqa: S506 (the control for the test)
    except Exception:  # noqa: BLE001, S110
        pass
    assert marker.exists()
