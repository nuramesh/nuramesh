"""Trust-list maintenance for signer keys (spec `spec/verified-merge` SV-6, SV-7, 6.7, FR-8;
Fable fixes F-7 holder, F-12 flip protocol).

Pure functions over `vm_formats.TrustList`: each change returns the next list (`serial + 1`,
the input untouched) and `key_changed_payload` the `merge.key_changed` payload (serials, the
hashes of both lists, the key id, holder, role and the kind of change: ids and hashes only, never
a public key, never free text). Who may commit the list (an admin, a tier-3 PR, break-glass for
the signer's own key) is the gate's and the CLI's business; this module only makes the objects
right. `signer_key_status` is the signer's own check of its key (role `verdict-signer`, `holder`
equal to `service:<reviewer id>` (F-7), not compromised, not revoked, inside its window) and
`accept_trust` the serial rule (a lower serial, or a different list at the same serial, is
`trust_rollback`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from nuramesh.core.vm_formats import (
    TrustKey,
    TrustList,
    format_time,
    parse_time,
    payload_hash,
    public_key_id,
    refuse_key_id_collisions,
)

KEY_STATUS_CODES = (
    "key_not_in_trust_list",
    "key_entry_invalid",
    "key_role",
    "key_holder",
    "key_compromised",
    "key_revoked",
    "key_not_yet_valid",
    "key_expired",
)
Change = Literal["added", "rotated", "revoked", "compromised"]
CHANGES: tuple[str, ...] = ("added", "rotated", "revoked", "compromised")


def signer_key_status(trust: TrustList, key_id: str, reviewer_id: str, now: datetime) -> str | None:
    """None when `key_id` may sign verdicts for `reviewer_id` at `now`, else a `KEY_STATUS_CODES`
    code, in the order the gate would refuse (GT-10, SV-7)."""
    key = trust.key(key_id)
    if key is None:
        return "key_not_in_trust_list"
    problem = key.problem()
    if problem == "revoked_without_seq" or problem == "key_id":
        return "key_entry_invalid"
    if key.role != "verdict-signer":
        return "key_role"
    if problem is not None or key.holder != "service:" + reviewer_id:
        return "key_holder"
    if key.compromised:
        return "key_compromised"
    if key.revoked_seq is not None:
        return "key_revoked"
    if parse_time(key.valid_from) > now:
        return "key_not_yet_valid"
    if key.valid_to is not None and now >= parse_time(key.valid_to):
        return "key_expired"
    return None


def accept_trust(current: TrustList | None, new: TrustList) -> str | None:
    """None when `new` may replace `current`: the same org, and a higher serial or the very same
    list; else `trust_org` or `trust_rollback` (6.7: the serial only increases)."""
    if current is None:
        return None
    if new.org_id != current.org_id:
        return "trust_org"
    if new.serial < current.serial:
        return "trust_rollback"
    if new.serial == current.serial and payload_hash(new) != payload_hash(current):
        return "trust_rollback"
    return None


def _with_keys(trust: TrustList, keys: list[TrustKey]) -> TrustList:
    return trust.model_copy(update={"serial": trust.serial + 1, "keys": keys})


def _replace(trust: TrustList, key_id: str, **update: object) -> list[TrustKey]:
    if trust.key(key_id) is None:
        raise ValueError("key id is not in the trust list")
    return [k.model_copy(update=update) if k.key_id == key_id else k for k in trust.keys]


def add_signer_key(
    trust: TrustList,
    public_key_hex: str,
    *,
    holder: str,
    now: datetime,
    reserved: Mapping[str, str],
) -> TrustList:
    """The next list with a new `verdict-signer` key (custody `service`, valid from `now`) for
    `holder` (`service:<reviewer id>`, F-7). Refuses a duplicate key id and a key id of another
    space (`KeyIdCollision`)."""
    if not holder.startswith("service:"):
        raise ValueError("a verdict-signer key is held by a service: identity")
    key_id = public_key_id(public_key_hex)
    if trust.key(key_id) is not None:
        raise ValueError("key id is already in the trust list")
    entry = TrustKey(
        key_id=key_id,
        role="verdict-signer",
        holder=holder,
        roles=[],
        public_key=public_key_hex,
        custody="service",
        valid_from=format_time(now),
        valid_to=None,
        revoked_at=None,
        revoked_seq=None,
        compromised=False,
    )
    new = _with_keys(trust, [*trust.keys, entry])
    refuse_key_id_collisions(new, reserved)
    return new


def rotate_signer_key(
    trust: TrustList,
    old_key_id: str,
    new_public_key_hex: str,
    *,
    now: datetime,
    reserved: Mapping[str, str],
) -> TrustList:
    """SV-7 rotation: the new key takes the old key's holder from `now`; the old key's window is
    left as it is (the gate checks the window at `t = now`, GT-10, so closing it would void the
    open PRs' verdicts): the old key is retired by `revoke_key` with the ledger sequence of the
    `merge.key_changed` entry, which keeps every verdict issued before it valid."""
    old = trust.key(old_key_id)
    if old is None:
        raise ValueError("key id is not in the trust list")
    return add_signer_key(trust, new_public_key_hex, holder=old.holder, now=now, reserved=reserved)


def revoke_key(
    trust: TrustList, key_id: str, *, revoked_at: datetime, revoked_seq: int
) -> TrustList:
    """SV-7: mark `key_id` revoked at the ledger sequence of the revoking `merge.key_changed`
    entry; verdicts whose issue entry has a higher sequence are invalid, earlier ones stay. The
    window is not touched: revocation is judged by ledger order, never by time."""
    if revoked_seq < 1:
        raise ValueError("revoked_seq is a ledger sequence number")
    return _with_keys(
        trust, _replace(trust, key_id, revoked_at=format_time(revoked_at), revoked_seq=revoked_seq)
    )


def mark_compromised(trust: TrustList, key_id: str) -> TrustList:
    """SV-7 / F-12: every verdict of the key becomes invalid, earlier ones included (the flip
    protocol marks the shadow signer key this way)."""
    return _with_keys(trust, _replace(trust, key_id, compromised=True))


def key_changed_payload(
    previous: TrustList, new: TrustList, *, change: str, key_id: str
) -> dict[str, str | int]:
    """The `merge.key_changed` payload (LA-1): serials, both list hashes, the key id, holder,
    role and the change kind. Refuses a serial that is not `previous + 1` and a key id the new
    list does not hold."""
    if change not in CHANGES:
        raise ValueError("unknown trust change kind")
    if new.serial != previous.serial + 1 or new.org_id != previous.org_id:
        raise ValueError("the new trust list is not the next serial of the previous one")
    key = new.key(key_id)
    if key is None:
        raise ValueError("key id is not in the new trust list")
    return {
        "org_id": new.org_id,
        "serial": new.serial,
        "previous_serial": previous.serial,
        "trust_sha256": payload_hash(new),
        "previous_trust_sha256": payload_hash(previous),
        "key_id": key_id,
        "holder": key.holder,
        "role": key.role,
        "change": change,
    }
