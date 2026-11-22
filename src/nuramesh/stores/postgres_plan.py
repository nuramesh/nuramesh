"""The read-only plan for `nuramesh db migrate` (issue 897, LB1 and m7).

`plan_migrations` runs SELECTs only (never DDL, never creates the version table): which migrations
are pending, which of them are not additive (`postgres.NON_ADDITIVE_MIGRATIONS`), and which right
the connecting role lacks to apply them, by name. `acknowledgement` decides whether the
operator has said, before a non-additive migration, that a backup newer than the last upgrade
exists.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from importlib import resources

import psycopg

from nuramesh.stores import postgres
from nuramesh.stores.base import StoreError

ACK_ENV = "NURAMESH_MIGRATE_ACK"
_BACKUP_REF_MAX = 200


@dataclass(frozen=True)
class PendingMigration:
    """One migration the plan would apply."""

    number: int
    name: str
    additive: bool


@dataclass(frozen=True)
class MigrationPlan:
    """What `db migrate` would do to this database right now."""

    current: int
    target: int
    pending: tuple[PendingMigration, ...]
    missing_rights: tuple[str, ...]

    @property
    def non_additive(self) -> tuple[PendingMigration, ...]:
        """The pending migrations that rewrite existing rows or columns."""
        return tuple(m for m in self.pending if not m.additive)


def _names() -> dict[int, str]:
    try:
        text = resources.files("nuramesh.stores").joinpath("migration_names.txt").read_text("utf-8")
    except (OSError, ModuleNotFoundError):
        return {}
    names: dict[int, str] = {}
    for line in text.splitlines():
        number, _, name = line.partition(" ")
        if number.isdigit() and name:
            names[int(number)] = name.strip()
    return names


def non_additive_sentence(numbers: Collection[int]) -> str:
    """`7, 8 and 11`: the list as the upgrade notes write it (generated, never typed twice)."""
    ordered = sorted(numbers)
    if len(ordered) < 2:
        return ", ".join(str(n) for n in ordered)
    return ", ".join(str(n) for n in ordered[:-1]) + f" and {ordered[-1]}"


def plan_migrations(dsn: str) -> MigrationPlan:
    """The plan for the database at `dsn`. Read-only; raises `StoreError` when it cannot connect
    or the database is newer than this version (the message never holds the URL)."""
    target = len(postgres.MIGRATIONS)
    try:
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute("SET default_transaction_read_only = on")
            row = db.execute("SELECT to_regclass('schema_version')").fetchone()
            current = 0
            if row is not None and row[0] is not None:
                got = db.execute("SELECT MAX(version) FROM schema_version").fetchone()
                current = int(got[0] or 0) if got else 0
            if current > target:
                raise StoreError(f"database schema {current} is newer than this version ({target})")
            names = _names()
            pending = tuple(
                PendingMigration(
                    n,
                    names.get(n, f"migration {n}"),
                    n not in postgres.NON_ADDITIVE_MIGRATIONS,
                )
                for n in range(current + 1, target + 1)
            )
            missing = _missing_rights(db, current, target) if pending else ()
    except psycopg.Error as exc:
        raise StoreError(f"could not read the database ({type(exc).__name__})") from None
    return MigrationPlan(current, target, pending, missing)


def _missing_rights(
    db: "psycopg.Connection[tuple[object, ...]]", current: int, target: int
) -> tuple[str, ...]:
    missing: list[str] = []
    who = str(db.execute("SELECT current_user").fetchone()[0])  # type: ignore[index]
    schema_row = db.execute(
        "SELECT current_schema(), has_schema_privilege(current_user, current_schema(), 'CREATE')"
    ).fetchone()
    if schema_row is not None and not schema_row[1]:
        missing.append(f"role {who} lacks CREATE on schema {schema_row[0]}")
    owner = db.execute(
        "SELECT pg_get_userbyid(c.relowner), pg_has_role(current_user, c.relowner, 'USAGE') "
        "FROM pg_class c WHERE c.oid = to_regclass('ledger_events')"
    ).fetchone()
    if owner is not None and not owner[1]:
        missing.append(
            f"role {who} is not the table owner {owner[0]} nor a member of it: "
            f"run the migration as {owner[0]} (docs/upgrade-notes.md)"
        )
    needs_definer = any(current < since <= target for _, since in postgres.DEFINER_FUNCTIONS)
    if needs_definer:
        role = db.execute(
            "SELECT pg_has_role(current_user, 'nuramesh_definer', 'MEMBER') "
            "FROM pg_roles WHERE rolname = 'nuramesh_definer'"
        ).fetchone()
        if role is None:
            missing.append(
                "the role nuramesh_definer does not exist; create it first (docs/upgrade-notes.md)"
            )
        elif not role[0]:
            missing.append(
                f"role {who} is not a member of nuramesh_definer: "
                f"GRANT nuramesh_definer TO {who} (docs/upgrade-notes.md)"
            )
    return tuple(missing)


def acknowledgement(
    plan: MigrationPlan, backup: str | None, env: Mapping[str, str]
) -> tuple[str | None, str | None]:
    """`(reference, problem)` for the rule: while a non-additive migration is pending the operator
    must pass `--i-have-a-backup REF` or set `NURAMESH_MIGRATE_ACK` to the highest pending number.
    No pending non-additive migration, or an empty database (version 0: there are no rows to
    rewrite, a fresh install): `(None, None)`, an acknowledgement is not needed."""
    if not plan.non_additive or plan.current == 0:
        return None, None
    top = plan.pending[-1].number
    listed = ", ".join(str(m.number) for m in plan.non_additive)
    how = (
        f"pending migration(s) {listed} are not additive: they rewrite existing rows, and rolling "
        "back means restoring a backup. Take the backup (https://docs.nuramesh.com/guides/backup-and-restore/), stop "
        "every process that uses the database, then run `nuramesh db migrate --i-have-a-backup "
        f"<dump file or timestamp>` or set {ACK_ENV}={top}. `nuramesh db migrate --plan` shows "
        "what would run."
    )
    if backup is not None:
        ref = backup.strip()
        if not ref or len(ref) > _BACKUP_REF_MAX or not ref.isprintable():
            return None, (
                f"--i-have-a-backup needs a short printable reference (a dump file or a "
                f"timestamp, at most {_BACKUP_REF_MAX} characters)"
            )
        return ref, None
    value = env.get(ACK_ENV, "").strip()
    if value == str(top):
        return f"env:{ACK_ENV}={top}", None
    if value:
        return (
            None,
            f"{ACK_ENV}={value} does not name the highest pending migration ({top}); " + how,
        )
    return None, "refusing to migrate: " + how


def render(plan: MigrationPlan) -> str:
    """The text `db migrate --plan` prints."""
    lines = [f"schema at version {plan.current}; target version {plan.target}"]
    if not plan.pending:
        lines.append("nothing pending")
    for m in plan.pending:
        lines.append(f"  {m.number} {m.name}: {'additive' if m.additive else 'NOT ADDITIVE'}")
    if plan.non_additive and plan.current == 0:
        lines.append("empty database: a fresh install, no acknowledgement needed")
    elif plan.non_additive:
        lines.append(
            "a not-additive migration is pending: back up first, stop every process that uses the "
            "database, and acknowledge with --i-have-a-backup REF (or "
            f"{ACK_ENV}={plan.pending[-1].number})"
        )
    for problem in plan.missing_rights:
        lines.append(f"MISSING RIGHT: {problem}")
    return "\n".join(lines)
