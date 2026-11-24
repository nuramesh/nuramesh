"""Template versions as YAML files in a Git working copy (T39)."""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("yaml")

from nuramesh.config import (  # noqa: E402
    ConfigError,
    load_template_versions,
    save_template_file,
    template_from_yaml,
    template_path,
    template_to_yaml,
)
from nuramesh.core.config import parse_config  # noqa: E402
from nuramesh.core.templates import (  # noqa: E402
    TemplateVersion,
    config_from_template,
    save_as_template,
)

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
ORG: dict[str, Any] = {
    "id": "acme",
    "name": "Acme",
    "roles": [
        {"id": "cos", "name": "Chief of Staff", "kind": "chief_of_staff"},
        {"id": "dev", "name": "Developer: NO"},  # text that YAML 1.1 would mangle
    ],
    "agents": [
        {"id": "boss", "name": "yes", "role": "cos"},
        {"id": "dev", "name": "1:30", "role": "dev", "reports_to": "boss"},
    ],
}


def version(**kw: Any) -> TemplateVersion:
    cfg = parse_config(
        {
            "org": ORG,
            "budgets": [
                {
                    "id": "b",
                    "scope": "agent",
                    "scope_id": "dev",
                    "limit": {"amount": "2.10", "currency": "USD"},
                }
            ],
        }
    )
    made, _ = save_as_template(cfg, template_id="team", name="Team é", saved_at=NOW, **kw)
    return made


def test_yaml_round_trips_exactly_including_awkward_text_and_money() -> None:
    v1 = version(description="Line one\nLine two")
    text = template_to_yaml(v1)
    assert template_to_yaml(v1) == text  # stable, so a file never churns in Git
    back = template_from_yaml(text)
    assert back == v1 and back.document_hash == v1.document_hash
    used = config_from_template(back, org_id="beta", org_name="Beta")
    assert [a.name for a in used.org.agents] == ["yes", "1:30"]
    assert str(used.budgets[0].limit.amount) == "2.10"


def test_a_hand_edited_document_is_refused() -> None:
    text = template_to_yaml(version())
    with pytest.raises(ConfigError, match="hashes"):
        template_from_yaml(text.replace("Developer: NO", "Developer: YES"))
    with pytest.raises(ConfigError, match="expected exactly"):
        template_from_yaml("template: {id: x}\ndocument: {}\n")
    with pytest.raises(ConfigError):
        template_from_yaml("- not a mapping\n")


def test_files_are_written_once_and_read_back_in_order(tmp_path: Path) -> None:
    v1 = version()
    path = save_template_file(tmp_path, v1)
    assert path == template_path(tmp_path, "team", 1) == tmp_path / "team" / "v1.yaml"
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    with pytest.raises(FileExistsError):
        save_template_file(tmp_path, v1)  # a saved version is immutable
    cfg = parse_config({"org": ORG})
    v2, _ = save_as_template(cfg, template_id="team", name="Team é", saved_at=NOW, existing=[v1])
    save_template_file(tmp_path, v2)
    assert [v.version for v in load_template_versions(tmp_path, "team")] == [1, 2]
    assert load_template_versions(tmp_path, "nothing") == []


def test_a_file_named_for_another_version_or_template_is_refused(tmp_path: Path) -> None:
    v1 = version()
    save_template_file(tmp_path, v1)
    os.rename(tmp_path / "team" / "v1.yaml", tmp_path / "team" / "v7.yaml")
    with pytest.raises(ConfigError, match="file name"):
        load_template_versions(tmp_path, "team")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "v1.yaml").write_text(template_to_yaml(v1), "utf-8")
    with pytest.raises(ConfigError, match="file name"):
        load_template_versions(tmp_path, "other")


def test_symlinks_and_stray_files_are_ignored(tmp_path: Path) -> None:
    v1 = version()
    save_template_file(tmp_path, v1)
    folder = tmp_path / "team"
    (folder / "notes.txt").write_text("hi", "utf-8")
    (folder / "v2.yaml").symlink_to(folder / "v1.yaml")  # would read v1 under the name v2
    assert [v.version for v in load_template_versions(tmp_path, "team")] == [1]


@pytest.mark.parametrize(
    "awkward", ["3e4", "1E-7", "+1e3", "2e10", "0x1F", "null", "~", "True", "007", ".5", "-3", ""]
)
def test_text_the_loader_would_read_as_something_else_round_trips(
    awkward: str, tmp_path: Path
) -> None:
    cfg = parse_config(
        {"org": {**ORG, "agents": [{**ORG["agents"][0], "name": awkward or "x"}, ORG["agents"][1]]}}
    )
    v1, _ = save_as_template(
        cfg, template_id="team", name=awkward or "n", saved_at=NOW, description=awkward
    )
    save_template_file(tmp_path, v1)
    assert load_template_versions(tmp_path, "team") == [v1]


def test_editing_a_name_or_description_by_hand_is_noticed(tmp_path: Path) -> None:
    path = save_template_file(tmp_path, version(description="original"))
    text = path.read_text("utf-8")
    for old, new in (("original", "changed"), ("Team é", "Team E")):
        path.write_text(text.replace(old, new), "utf-8")
        with pytest.raises(ConfigError, match="hashes"):
            load_template_versions(tmp_path, "team")


def test_a_failed_save_leaves_no_partial_version_and_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    v1 = version()

    def boom(*_: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(OSError):
        save_template_file(tmp_path, v1)
    monkeypatch.undo()
    assert list((tmp_path / "team").iterdir()) == []
    save_template_file(tmp_path, v1)  # and it can still be saved afterwards
    assert [p.name for p in (tmp_path / "team").iterdir()] == ["v1.yaml"]


def test_ids_that_are_not_slugs_and_symlinked_folders_are_refused(tmp_path: Path) -> None:
    for bad in ("../x", "A", "", "a/b"):
        with pytest.raises(ValueError):
            load_template_versions(tmp_path, bad)
        with pytest.raises(ValueError):
            template_path(tmp_path, bad, 1)
    with pytest.raises(ValueError):
        template_path(tmp_path, "ok", 0)
    real = tmp_path / "elsewhere"
    real.mkdir()
    (tmp_path / "team").symlink_to(real)
    with pytest.raises(ValueError, match="symbolic link"):
        save_template_file(tmp_path, version())
    assert load_template_versions(tmp_path, "team") == []


def test_a_saved_gate_preset_with_config_survives_yaml(tmp_path: Path) -> None:
    cfg = parse_config(
        {
            "org": ORG,
            "presets": {
                "gate": [
                    {
                        "id": "py",
                        "name": "Python",
                        "gates": [
                            {
                                "id": "t",
                                "kind": "test_command",
                                "config": {"command": ["pytest", "3e4"]},
                            }
                        ],
                    }
                ],
                "assignments": [{"role": "dev", "gate_preset": "py"}],
            },
        }
    )
    v1, _ = save_as_template(cfg, template_id="team", name="Team", saved_at=NOW)
    save_template_file(tmp_path, v1)
    (back,) = load_template_versions(tmp_path, "team")
    used = config_from_template(back, org_id="b", org_name="B")
    assert used.presets == cfg.presets
