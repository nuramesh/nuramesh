"""The owner-run key commands (R1.37): `content shred`, `keys status`, `keys reconcile` and
`keys adopt-legacy`. No HTTP route does any of this (an erase button needs its own authentication
design). Each opens the team's store like `nuramesh stop` does and uses the default file key ring
of that store (`<NURAMESH_KEY_DIR>/<store id>/`)."""

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from nuramesh.cli_prompts import confirm_org
from nuramesh.cli_rules import add_team
from nuramesh.core.people import is_human_id
from nuramesh.engine.content import (
    ContentError,
    ContentService,
    configured_content_service,
)
from nuramesh.stores.base import StoreError

OK, USAGE, REFUSED = 0, 2, 3

STATE_TEXT = {
    1: "a new organization, or never keyed",
    2: "a live organization (key present, no tombstone)",
    3: "destroyed, healthy (both tombstones, no key)",
    4: "destroyed, but a key file came back beside both tombstones (never loaded)",
    5: "destroyed in the database; the key folder has no tombstone copy",
    6: "destroyed in the database; a key file is present and the folder has no tombstone copy",
    7: "a tombstone file only: the database does not know it (restored, or a shred cut short)",
    8: "a tombstone file and a key file; the database does not know it",
}


def _actor(args: argparse.Namespace) -> str | None:
    human = getattr(args, "human", None)
    if human is None:
        return "service:cli"
    if not is_human_id(human):
        print("--human needs an opaque id like human:u_7k2x9q (no email or name)", file=sys.stderr)
        return None
    return str(human)


def _open(args: argparse.Namespace, store_factory: Callable[[Path, str | None], Any]) -> Any:
    """`(store, service)`; `(None, None)` when the store can't be opened (nothing is claimed)."""
    try:
        store = store_factory(Path(args.team).resolve(), args.store)
    except ImportError:
        print("NURAMESH_DATABASE_URL needs: pip install 'nuramesh[postgres]'", file=sys.stderr)
        return None, None
    except StoreError as exc:
        print(f"couldn't open the database: {exc}", file=sys.stderr)  # never includes the URL
        return None, None
    try:
        return store, configured_content_service(store)
    except ContentError as exc:
        print(f"the content store isn't available: {exc}", file=sys.stderr)
        return store, None


KEYS_PENDING_TEXT = "keys pending: the sweeper finishes the destruction"


def _attach_key_erasure(store: Any, service: ContentService) -> bool:
    """R1.14c: attach the org-erasure hook when `NURAMESH_SECRET_DIR` is set (the same store
    `serve` uses). No store configured: the hook stays off and nothing changes. Returns True when
    a store is configured but cannot be opened (the shred still runs; the sweeper finishes)."""
    from nuramesh.cli import secret_store_from_env
    from nuramesh.core.secret_store import SecretStoreError
    from nuramesh.engine.key_sweeper import OrgErasure

    try:
        secrets_store = secret_store_from_env(os.environ)
    except SecretStoreError as exc:
        return exc.detail != "hosted_needs_real_store"  # no store named: nothing to erase here
    if secrets_store is None:
        return False
    OrgErasure(store, secrets_store).attach(service)
    return False


def cmd_content_shred(
    args: argparse.Namespace, store_factory: Callable[[Path, str | None], Any]
) -> int:
    """Destroy an organization's content key and leave the permanent tombstone."""
    actor = _actor(args)
    if actor is None:
        return USAGE
    asked = confirm_org(args, args.org, "content shred")  # OD-14, BC9: no terminal needs --yes
    if asked is not None:
        return asked
    store, service = _open(args, store_factory)
    if service is None:
        return USAGE
    pending = _attach_key_erasure(store, service)
    try:
        destroyed, hook_pending = service.shred_reporting(args.org, actor=actor, reason=args.reason)
        pending = pending or hook_pending
    except ContentError as exc:
        print(str(exc), file=sys.stderr)
        return REFUSED
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return USAGE
    if destroyed:
        print(f"the key of '{args.org}' was destroyed; its content can no longer be read")
    else:
        print(f"'{args.org}' is erased (no key was left to destroy); the tombstone is in place")
    if pending:
        print(KEYS_PENDING_TEXT)  # a fixed line: no path, no key text
    return OK


def _folder(service: ContentService) -> str:
    return str(getattr(service.keys, "directory", "(memory)"))


def cmd_keys_status(
    args: argparse.Namespace, store_factory: Callable[[Path, str | None], Any]
) -> int:
    """Say which of the eight states an organization is in (all tombstoned ones if none is
    named). Opens no key. Writes nothing except the coalesced `content.key_resurrected` alert row
    for a key present beside a tombstone."""
    _store, service = _open(args, store_factory)
    if service is None:
        return USAGE
    orgs = (
        [args.org]
        if args.org
        else sorted(
            {t.org_id for t in service.store.key_tombstones()} | set(service.keys.tombstoned_orgs())
        )
    )
    print(f"key folder: {_folder(service)}")
    for org in orgs:
        try:
            state = service.tombstone_state(org)
        except ContentError as exc:
            print(str(exc), file=sys.stderr)
            return USAGE
        print(f"{org}: state {state}: {STATE_TEXT[state]}")
        if state in (4, 6, 8):
            print("key present: never loaded")
            service.note_resurrected(org)
    if not orgs:
        print("no organization has a tombstone")
    return OK


def cmd_keys_reconcile(
    args: argparse.Namespace, store_factory: Callable[[Path, str | None], Any]
) -> int:
    """`--adopt`: record a ring tombstone the database does not know (state 7).
    `--destroy-resurrected`: destroy a key that came back beside a tombstone. Idempotent."""
    actor = _actor(args)
    if actor is None:
        return USAGE
    _store, service = _open(args, store_factory)
    if service is None:
        return USAGE
    org = args.org
    try:
        state = service.tombstone_state(org)
    except ContentError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    if state in (1, 2):
        print(f"'{org}' is not tombstoned", file=sys.stderr)
        return USAGE
    if args.adopt:
        if state in (7, 8):
            if service.adopt_tombstone(org, actor=actor):
                print(f"'{org}': the tombstone was recorded in the database")
            else:
                print(f"nothing to do: '{org}' is already recorded")
            return OK
        print(f"nothing to do: the database already records '{org}'")
        return OK
    if state in (7, 8):
        print(
            "no store tombstone: run `nuramesh keys reconcile --adopt "
            f"--org {org}` or `nuramesh content shred --org {org}` first",
            file=sys.stderr,
        )
        return USAGE
    if state in (4, 6) and service.destroy_resurrected(org, actor=actor):
        print(f"the key file that came back for '{org}' was destroyed")
        return OK
    print(f"nothing to do: no key file for '{org}'")
    return OK


def cmd_keys_adopt_legacy(
    args: argparse.Namespace, store_factory: Callable[[Path, str | None], Any]
) -> int:
    """Move a key from before the per-store folders into this store's folder, once."""
    actor = _actor(args)
    if actor is None:
        return USAGE
    _store, service = _open(args, store_factory)
    if service is None:
        return USAGE
    try:
        outcome = service.adopt_legacy(args.org, actor=actor)
    except ContentError as exc:
        print(str(exc), file=sys.stderr)
        return USAGE
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return USAGE
    org = args.org
    if outcome == "adopted":
        print(f"the legacy key of '{org}' now belongs to this store")
        return OK
    if outcome == "tombstoned":
        print(f"organization '{org}' was erased; its key is not adopted", file=sys.stderr)
        return REFUSED
    if outcome == "exists":
        print(f"a key for '{org}' already exists in this store; nothing was moved", file=sys.stderr)
        return REFUSED
    print(f"no legacy key for '{org}'", file=sys.stderr)
    return USAGE


def add_parsers(sub: Any, store_factory: Callable[[Path, str | None], Any]) -> Any:
    """Register `content shred` and `keys status|reconcile|adopt-legacy`; returns the `content`
    subparsers so `content expire` (cli.py) joins the same group."""

    def common(p: argparse.ArgumentParser, *, human: bool) -> None:
        add_team(p)
        p.add_argument("--store")
        if human:
            p.add_argument("--human", metavar="HUMAN", help="who is doing it: human:<id>")

    content = sub.add_parser("content", help="owner actions on the content store")
    csub = content.add_subparsers(dest="content_command", required=True)
    shred = csub.add_parser(
        "shred", help="destroy an organization's key for good and leave a permanent tombstone"
    )
    shred.add_argument("--org", required=True, help="the organization whose key is destroyed")
    shred.add_argument("--reason", choices=["shred", "erasure"], default="shred")
    shred.add_argument(
        "--confirm",
        metavar="ORG",
        help="an extra check: must equal --org when given (the question is still asked on a "
        "terminal; --yes skips it)",
    )
    common(shred, human=True)
    shred.set_defaults(func=lambda a: cmd_content_shred(a, store_factory))

    keys = sub.add_parser("keys", help="inspect and repair organization keys and tombstones")
    ksub = keys.add_subparsers(dest="keys_command", required=True)
    status = ksub.add_parser("status", help="the state of an organization's key and tombstones")
    status.add_argument("--org", help="one organization (default: every tombstoned one)")
    common(status, human=False)
    status.set_defaults(func=lambda a: cmd_keys_status(a, store_factory))
    rec = ksub.add_parser("reconcile", help="repair a disagreement between the two tombstones")
    group = rec.add_mutually_exclusive_group(required=True)
    group.add_argument("--adopt", action="store_true")
    group.add_argument("--destroy-resurrected", action="store_true")
    rec.add_argument("--org", required=True, help="the organization to repair")
    common(rec, human=True)
    rec.set_defaults(func=lambda a: cmd_keys_reconcile(a, store_factory))
    adopt = ksub.add_parser("adopt-legacy", help="move a key from before per-store folders here")
    adopt.add_argument("--org", required=True, help="the organization whose key moves")
    common(adopt, human=True)
    adopt.set_defaults(func=lambda a: cmd_keys_adopt_legacy(a, store_factory))
    return csub
