"""Recording configuration changes in the ledger: renames, preset edits, saved templates (T39).

Names, presets and templates are configuration, so the ledger records *that* they changed, by whom
and when (the entry's `actor` and `at`), with ids and hashes, never content (A7). Entries go on
the organization's stream (`org:<id>`) as `decision.recorded` with a `kind`:

- `rename`: what, id, old and new name;
- `preset_change`: which preset was created, edited or removed, its old and new hash and the roles
  (so the agents) it affects, one entry per preset;
- `preset_assignment`: a role's presets changed (a preset applied to or removed from a role);
- `template_saved`: template id, version, name and document hash.

Each function writes its entries in **one transaction** and returns them, so the caller can show
the ledger entry ids (SPEC-app-v1 §4: every mutating call returns the resulting ledger entry).
"""

from nuramesh.core.config import NuraMeshConfig
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.core.presets import (
    PresetConfig,
    assignment_details,
    preset_changes,
    preset_renames,
)
from nuramesh.core.renames import RenameKind, RenameRecord, check_ids_stable, rename_in_config
from nuramesh.core.templates import TemplateVersion
from nuramesh.engine.evals import org_stream
from nuramesh.stores import EventDraft, Store


def _record(
    store: Store, org_id: str, actor: str, payloads: list[dict[str, object]]
) -> list[LedgerEvent]:
    with store.transaction() as uow:
        return [
            uow.append(
                EventDraft(
                    stream=org_stream(org_id),
                    type=EventType.DECISION_RECORDED,
                    actor=actor,
                    payload=payload,
                )
            )
            for payload in payloads
        ]


def rename(
    store: Store,
    config: NuraMeshConfig,
    kind: RenameKind,
    item_id: str,
    new_name: str,
    *,
    actor: str,
) -> tuple[NuraMeshConfig, LedgerEvent]:
    """Rename one thing: the new configuration and the ledger entry (who, when, old → new).

    The configuration is not stored here; the caller keeps it. If recording fails nothing was
    renamed (the caller never sees the new configuration).
    """
    changed, record = rename_in_config(config, kind, item_id, new_name)
    (event,) = _record(store, config.org.id, actor, [record.ledger_payload()])
    return changed, event


def record_rename(store: Store, org_id: str, record: RenameRecord, *, actor: str) -> LedgerEvent:
    """Record a rename made elsewhere (for example the one `save_as_template` reports)."""
    (event,) = _record(store, org_id, actor, [record.ledger_payload()])
    return event


def update_presets(
    store: Store,
    config: NuraMeshConfig,
    presets: PresetConfig | None,
    *,
    actor: str,
) -> tuple[NuraMeshConfig, list[LedgerEvent]]:
    """Replace the presets: validated like any configuration, every change recorded.

    Editing a preset needs nothing more: agents use presets through their roles, so every agent
    holding a role that uses the edited preset follows it (`NuraMeshConfig.effective_policies`).
    Raises `ValueError` if the result isn't valid (an unknown preset or role, a loosened gate
    order, too many rules) or if an id that other things may reference would disappear while
    still in use. Returns the new configuration and one ledger entry per change.
    """
    changed = config.model_copy(update={"presets": presets})  # validated
    payloads: list[dict[str, object]] = [
        c.ledger_payload() for c in preset_changes(config.presets, presets)
    ]
    for what, preset_id, old_name, new_name in preset_renames(config.presets, presets):
        payloads.append(
            RenameRecord(
                kind=RenameKind(what), id=preset_id, old_name=old_name, new_name=new_name
            ).ledger_payload()
        )
    details = assignment_details(config.presets, presets)
    if details:
        payloads.append(
            {"kind": "preset_assignment", "roles": [d["role"] for d in details], "changes": details}
        )
    if not payloads:
        return changed, []
    return changed, _record(store, config.org.id, actor, payloads)


def record_template_saved(
    store: Store, org_id: str, version: TemplateVersion, *, actor: str
) -> LedgerEvent:
    """Record a saved template version (id, version, name, document hash)."""
    (event,) = _record(store, org_id, actor, [version.ledger_payload()])
    return event


__all__ = [
    "check_ids_stable",
    "record_rename",
    "record_template_saved",
    "rename",
    "update_presets",
]
