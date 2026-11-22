"""Saved organization templates: immutable, numbered versions (app spec §5; T39).

"Save as template" turns the current configuration into **version N** of a template with an
immutable id. A version captures what makes an organization reusable: the org (roles, agents,
names, models), policies, budgets, evals and presets. It leaves out what belongs to one deployment
or is global data: the canon source (a Git repository and commit), the price book and price
overrides, and people (a policy's approvers, T42). Of the budgets only agent and team budgets are
kept: run and task budgets name one run or task, which a template can't know. Versions are never
edited: saving again makes version N+1, and nothing is saved when nothing changed, so a version
number always means one exact
document (`document_hash`).

A template's name is editable. Each version carries the name it was saved under; saving with a new
name (even with an unchanged document) makes a new version and a `RenameRecord`, so a rename is
recorded like any other (`core.renames`). Using a template builds a fresh configuration from a
version under a new org id and name (`config_from_template`, validated like any configuration).

Core only builds and checks these documents. YAML text, files in a Git working copy and ledger
entries are made by `nuramesh.config` and `nuramesh.engine.changes`.
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from nuramesh.core.budgets import BudgetScope
from nuramesh.core.canonical import canonical_json, content_hash, freeze, thaw
from nuramesh.core.config import NuraMeshConfig, parse_config
from nuramesh.core.org import Name, Slug
from nuramesh.core.renames import RenameKind, RenameRecord

TEMPLATE_KEYS = ("org", "policies", "budgets", "evals", "presets")
"""The configuration parts a template keeps (canon, prices and price overrides are left out)."""
MAX_VERSIONS = 10_000
Description = Annotated[str, StringConstraints(max_length=2_000)]


class TemplateVersion(BaseModel):
    """One saved version of a template. Immutable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Slug
    version: int = Field(ge=1, le=MAX_VERSIONS)
    name: Name
    description: Description = ""
    saved_at: datetime
    document: Mapping[str, Any]

    @field_validator("document", mode="after")
    @classmethod
    def _frozen(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        """Read-only all the way down, so equal documents compare equal however they arrived."""
        frozen: Mapping[str, Any] = freeze(value)
        return frozen

    @model_validator(mode="after")
    def _document(self) -> Self:
        if self.saved_at.tzinfo is None:
            raise ValueError("saved_at needs a time zone")
        if "org" not in self.document:
            raise ValueError("a template document needs an org")
        people = _approver_paths(self.document)
        if people:
            raise ValueError(f"a template holds no people, but has approvers at {people}")
        extra = sorted(set(self.document) - set(TEMPLATE_KEYS))
        if extra:
            raise ValueError(f"a template can't hold {extra} (allowed: {list(TEMPLATE_KEYS)})")
        try:
            canonical_json(self.document)  # must hash the same everywhere (no floats)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"the document can't be hashed: {exc}") from None
        return self

    @property
    def content_hash(self) -> str:
        """SHA-256 of everything about this version (id, number, name, description, time and
        document), so a hand edit of any of them is noticed when the file is read back."""
        return content_hash(
            {
                "id": self.id,
                "version": self.version,
                "name": self.name,
                "description": self.description,
                "saved_at": self.saved_at.isoformat(),
                "document_hash": self.document_hash,
            }
        )

    @property
    def document_hash(self) -> str:
        """SHA-256 of the document: one version number always means one exact document."""
        return content_hash(self.document)

    def ledger_payload(self) -> dict[str, Any]:
        """The `decision.recorded` payload for saving this version (ids, name and hash)."""
        return {
            "kind": "template_saved",
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "document_hash": self.document_hash,
        }


def template_document(config: NuraMeshConfig) -> dict[str, Any]:
    """The part of a configuration a template keeps, as plain JSON-able data."""
    kept = tuple(b for b in config.budgets if b.scope in (BudgetScope.AGENT, BudgetScope.TEAM))
    shown = config.model_copy(update={"budgets": kept})
    full = shown.model_dump(mode="json", exclude_defaults=True)
    document = {key: full[key] for key in TEMPLATE_KEYS if key in full}
    document["org"] = shown.model_dump(mode="json")["org"]
    # people are not part of a reusable organization: a policy's approvers (human ids) stay with
    # the organization that named them, and a template holds roles and rules only (T42)
    policies = document.get("policies")
    if isinstance(policies, dict):
        for policy in policies.get("policies", []):
            policy.pop("approvers", None)
    presets = document.get("presets")
    if isinstance(presets, dict):
        for gate_preset in presets.get("gate", []):
            for gate in gate_preset.get("gates", []):
                config = gate.get("config")
                if isinstance(config, dict):
                    config.pop("approvers", None)
    return document


def _approver_paths(value: Any, path: str = "") -> list[str]:
    """Where an `approvers` key appears in a document (people don't belong in a template)."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key == "approvers" and item:
                found.append(f"{path}.{key}")
            found += _approver_paths(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found += _approver_paths(item, f"{path}[{index}]")
    return found


def roles_needing_approvers(config: NuraMeshConfig) -> list[str]:
    """Roles with a rule that asks a human but no named approvers (so *any* human may answer).

    A configuration made from a template has this for every role that asks, because a template
    holds no people (T42): choose approvers per organization after using one.
    """
    return sorted(
        policy.role
        for policy in config.effective_policies.policies
        if not policy.approvers and any(rule.decision.value == "ask" for rule in policy.rules)
    )


def check_versions(existing: Sequence[TemplateVersion]) -> TemplateVersion | None:
    """The latest of a template's versions, after checking they are v1…vN of one template."""
    if not existing:
        return None
    ids = {v.id for v in existing}
    if len(ids) != 1:
        raise ValueError(f"versions of different templates mixed together: {sorted(ids)}")
    numbers = sorted(v.version for v in existing)
    if numbers != list(range(1, len(numbers) + 1)):
        raise ValueError(f"template versions must be 1..N without gaps or repeats, got {numbers}")
    return max(existing, key=lambda v: v.version)


def save_as_template(
    config: NuraMeshConfig,
    *,
    template_id: str,
    name: str,
    saved_at: datetime,
    description: str = "",
    existing: Sequence[TemplateVersion] = (),
) -> tuple[TemplateVersion, RenameRecord | None]:
    """The next version of a template made from `config`, and the rename it implies (if any).

    `existing` holds the template's saved versions (empty for the first). Raises `ValueError` if
    nothing changed since the latest version (same document, same name and description), if
    `existing` isn't v1…vN of this template, or if a field is invalid.
    """
    latest = check_versions(existing)
    if latest is not None and latest.id != template_id:
        raise ValueError(f"these are versions of '{latest.id}', not '{template_id}'")
    candidate = TemplateVersion(
        id=template_id,
        version=1 if latest is None else latest.version + 1,
        name=name,
        description=description,
        saved_at=saved_at,
        document=template_document(config),
    )
    rename: RenameRecord | None = None
    if latest is not None:
        if (
            candidate.document_hash == latest.document_hash
            and candidate.name == latest.name
            and candidate.description == latest.description
        ):
            raise ValueError(f"nothing changed since version {latest.version}")
        if candidate.name != latest.name:
            rename = RenameRecord(
                kind=RenameKind.TEMPLATE,
                id=template_id,
                old_name=latest.name,
                new_name=candidate.name,
            )
    return candidate, rename


def config_from_template(version: TemplateVersion, *, org_id: str, org_name: str) -> NuraMeshConfig:
    """A new configuration from a template version, under its own org id and name.

    Raises `ConfigValidationError` listing every problem if the document (for example one read
    back from a file someone edited) isn't a valid configuration.
    """
    data: dict[str, Any] = thaw(version.document)
    org = dict(data["org"])
    org["id"], org["name"] = org_id, org_name
    data["org"] = org
    return parse_config(data)
