"""What a refusal could write, as one comparable value (shared by the CLI deny tests).

`shape` is every stream's entries (type, actor, payload with the time masked), the security log,
the task ids of every run and the budget ids of every balance row; `added` is the delta between two
shapes. A refusal that writes only a security-log row or only a budget row changes the value.
"""

from nuramesh.stores import MemoryStore, SqliteStore


def shape(store: MemoryStore | SqliteStore, org_id: str) -> dict[str, object]:
    """Every stream's entries (type and payload, no time) and the security log, org id masked."""

    def clean(payload: dict[str, object]) -> dict[str, object]:
        out = {k: v for k, v in payload.items() if k != "at"}
        return {k: ("RUN" if k == "run" else v) for k, v in out.items()}

    streams = {
        name.replace(org_id, "ORG"): [
            (e.type, e.actor, clean(dict(e.payload))) for e in store.read_stream(name)
        ]
        for name in sorted(store.streams())
    }
    security = [(r.type, r.actor, dict(r.details)) for r in store.list_security_log(org_id)]
    runs = {"r1"} | {
        str(e.payload["run"])
        for name in store.streams()
        for e in store.read_stream(name)
        if "run" in e.payload
    }  # the engine's run id, the CLI's random one, any run a refusal named
    tasks = sorted(t.task.id for r in sorted(runs) for t in store.tasks_for_run(r))
    balances = sorted(b.balance.budget.id for b in store.list_balances(org_id))
    return {"streams": streams, "security": security, "tasks": tasks, "balances": balances}


def added(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    """What a refusal added: the new tail of each stream, the new security rows, new tasks."""
    out: dict[str, object] = {}
    sb, sa = before["streams"], after["streams"]
    assert isinstance(sb, dict) and isinstance(sa, dict)
    for name, entries in sa.items():
        new = entries[len(sb.get(name, [])) :]
        if new:
            out[name] = new
    sec_b, sec_a = before["security"], after["security"]
    assert isinstance(sec_b, list) and isinstance(sec_a, list)
    # the rows themselves, not their number: a coalesced count bump keeps the length
    out["security"] = [r for r in sec_a if r not in sec_b]
    for key in ("tasks", "balances"):
        old, new = before[key], after[key]
        assert isinstance(old, list) and isinstance(new, list)
        out[key] = [x for x in new if x not in old]
    return out
