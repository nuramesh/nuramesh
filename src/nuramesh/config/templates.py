"""Template versions as YAML files, for a Git working copy (app spec §5; T39).

A saved template version is one file, `<directory>/<template id>/v<N>.yaml`:

    template: {id, version, name, description, saved_at, document_hash}
    document: {org, policies, budgets, evals, presets}   # what `core.templates` keeps

Files are written once and never overwritten (a version is immutable); committing them is how a
template reaches Git. Reading checks the file name against the content and recomputes
`document_hash`, so a hand-edited document is refused rather than trusted: edit the organization
and save a new version instead. The text is parsed with the same strict YAML rules as every other
configuration file (`nuramesh.config.loader`).
"""

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nuramesh.core.canonical import thaw
from nuramesh.core.templates import TemplateVersion

from .loader import _BOOL, _FLOAT, _INT, _NULL, ConfigError, _yaml_module, parse_yaml

_FILE = re.compile(r"^v([1-9][0-9]{0,4})\.yaml$")
_TEMPLATE_KEYS = (
    "id",
    "version",
    "name",
    "description",
    "saved_at",
    "document_hash",
    "content_hash",
)
_SPECIAL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\ufeff]")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def template_to_yaml(version: TemplateVersion) -> str:
    """The YAML text of a version (stable: keys sorted, so a version's file never churns)."""
    yaml = _yaml_module()
    data = {
        "template": {
            "id": version.id,
            "version": version.version,
            "name": version.name,
            "description": version.description,
            "saved_at": version.saved_at.isoformat(),
            "document_hash": version.document_hash,
            "content_hash": version.content_hash,
        },
        "document": thaw(version.document),
    }
    text: str = yaml.dump(
        data,
        Dumper=_dumper(yaml),
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
    return text


def _dumper(yaml: Any) -> Any:
    """A dumper that quotes every string the strict loader would read as something else.

    PyYAML follows YAML 1.1 and leaves `3e4` or `+1e3` unquoted, but the loader here (YAML 1.2
    style) reads those as numbers, so a name like "3e4" would come back as a number and fail.
    """

    class Dumper(yaml.SafeDumper):  # type: ignore[misc]
        pass

    def represent_str(dumper: Any, value: str) -> Any:
        # also quote text with line-break-like or control characters (PyYAML reads U+0085 as a
        # line break in plain scalars, so "A\x85B" would come back as "A B")
        ambiguous = any(p.match(value) for p in (_NULL, _BOOL, _INT, _FLOAT)) or bool(
            _SPECIAL.search(value)
        )
        return dumper.represent_scalar(
            "tag:yaml.org,2002:str", value, style='"' if ambiguous else None
        )

    Dumper.add_representer(str, represent_str)
    return Dumper


def template_from_yaml(text: str, *, source: str = "<template>") -> TemplateVersion:
    """Parse a version, verifying its `document_hash`. Raises `ConfigError` on any problem."""
    data = parse_yaml(text, source=source)
    meta: Any = data.get("template")
    if (
        not isinstance(meta, dict)
        or set(data) != {"template", "document"}
        or set(meta) != set(_TEMPLATE_KEYS)
    ):
        raise ConfigError(
            f"{source}: expected exactly `template` ({', '.join(_TEMPLATE_KEYS)}) and `document`"
        )
    try:
        saved_at = datetime.fromisoformat(str(meta["saved_at"]))
        version = TemplateVersion(
            id=meta["id"],
            version=meta["version"],
            name=meta["name"],
            description=meta["description"],
            saved_at=saved_at,
            document=data["document"],
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise ConfigError(f"{source}: not a valid template version: {exc}") from exc
    if (
        version.document_hash != meta["document_hash"]
        or version.content_hash != meta["content_hash"]
    ):
        raise ConfigError(
            f"{source}: the file doesn't match its hashes (edited by hand?); "
            "save a new version from the organization instead"
        )
    return version


def template_path(directory: str | Path, template_id: str, version: int) -> Path:
    """Where a version lives: `<directory>/<template id>/v<N>.yaml`."""
    if not _SLUG.match(template_id) or version < 1:
        raise ValueError(f"'{template_id[:40]}' v{version} isn't a template version")
    return Path(directory) / template_id / f"v{version}.yaml"


def save_template_file(directory: str | Path, version: TemplateVersion) -> Path:
    """Write a version's file. Never overwrites: `FileExistsError` if it already exists."""
    path = template_path(directory, version.id, version.version)
    if path.parent.is_symlink():
        raise ValueError(f"{path.parent} is a symbolic link; refusing to write through it")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = template_to_yaml(version)
    # Written whole to a temporary file, then linked into place: an interrupted save leaves no
    # half-written version behind, and `link` fails if the version already exists (immutable).
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".saving-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.link(temporary, path)
    finally:
        os.unlink(temporary)
    return path


def load_template_versions(directory: str | Path, template_id: str) -> list[TemplateVersion]:
    """Every saved version of a template, oldest first; `[]` if there are none.

    Each file must be named for what it holds (`v3.yaml` holds version 3 of this template).
    """
    folder = Path(directory) / template_id
    if not _SLUG.match(template_id):
        raise ValueError(f"'{template_id[:40]}' isn't a template id")
    if folder.is_symlink() or not folder.is_dir():
        return []
    found: list[TemplateVersion] = []
    for path in sorted(folder.iterdir()):
        match = _FILE.match(path.name)
        if match is None or not path.is_file() or path.is_symlink():
            continue
        version = template_from_yaml(path.read_text("utf-8"), source=str(path))
        if version.id != template_id or version.version != int(match.group(1)):
            raise ConfigError(f"{path}: the file name doesn't match the template and version in it")
        found.append(version)
    return sorted(found, key=lambda v: v.version)
