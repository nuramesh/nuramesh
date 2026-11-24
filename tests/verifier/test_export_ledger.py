"""`nuramesh ledger export` writes what the standalone verifier reads, and the two verifiers agree
on a ledger the real anchoring code signed (ND94)."""

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from verify_helpers import load_verifier

pytest.importorskip("cryptography")

from nuramesh.cli import main  # noqa: E402
from nuramesh.core.ledger import EventType  # noqa: E402
from nuramesh.engine.anchors import export_heads, generate_key, load_private_key  # noqa: E402
from nuramesh.stores import EventDraft, SqliteStore  # noqa: E402

verifier = load_verifier()
ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@x",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@x",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=ENV, check=True)


class World:
    """A store with two streams, a signed anchor repository and its public key."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = root / "runs.db"
        self.store = SqliteStore(str(self.db))
        self.out = root / "ledger.jsonl"
        self.repo = root / "anchors"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "ledger-anchors")
        (self.repo / "README.md").write_text("ledger anchors\n", "utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-qm", "init")
        private, self.public = root / "keys" / "a.key", root / "keys" / "a.pub"
        private.parent.mkdir()
        generate_key(private, self.public)
        self.private = private

    def add(self, stream: str, count: int, start: int = 0) -> None:
        for i in range(start, start + count):
            with self.store.transaction() as uow:
                uow.append(
                    EventDraft(
                        stream=stream,
                        type=EventType.RUN_STARTED,
                        actor="system",
                        run_id=stream.split(":")[1],
                        payload={"n": i, "text": "é😀  ", "nested": {"b": [1, None, True]}},
                    )
                )

    def anchor(self) -> None:
        export_heads(self.store, self.repo, load_private_key(self.private))
        git(self.repo, "add", "-A")
        git(self.repo, "-c", "commit.gpgsign=false", "commit", "-qm", "anchors", "--allow-empty")

    def export(self) -> int:
        return main(
            [
                "ledger",
                "export",
                "--store",
                str(self.db),
                "--out",
                str(self.out),
                "--team",
                str(self.root),
            ]
        )

    def check(self, *extra: str) -> int:
        code: int = verifier.main(
            [str(self.out), "--anchors", str(self.repo), "--pubkey", str(self.public), *extra]
        )
        return code


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = World(tmp_path)
    try:
        w.add("run:r1", 4)
        w.add("org:o1", 2)
        yield w
    finally:
        w.store.close()


def test_export_is_one_stored_event_per_line_and_the_verifier_accepts_it(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.export() == 0
    lines = world.out.read_text("utf-8").splitlines()
    assert len(lines) == 6 and world.out.read_text("utf-8").endswith("\n")
    first = json.loads(lines[0])
    assert set(first) == {
        "stream", "seq", "type", "actor", "at", "run_id", "task_id", "payload", "cost",
        "cost_basis", "prev_hash", "hash",
    }  # fmt: skip
    assert first["at"].endswith("Z") and len(first["at"]) == 27
    assert verifier.main([str(world.out)]) == 0
    assert "0 broken" in _verify_ledger(world, capsys)


def _verify_ledger(world: World, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["ledger", "verify", "--store", str(world.db), "--team", str(world.root)]) == 0
    return capsys.readouterr().out


def test_real_signed_anchors_verify_with_the_standalone_code(world: World) -> None:
    world.anchor()
    assert world.export() == 0
    assert world.check("--require-anchors") == 0


def test_every_kind_of_tampering_with_the_export_is_caught(world: World) -> None:
    world.anchor()
    assert world.export() == 0
    original = world.out.read_text("utf-8")
    lines = original.splitlines(keepends=True)
    edits = {
        "edited": original.replace('"n":1', '"n":9', 1),
        "deleted": "".join(lines[:1] + lines[2:]),
        "truncated": "".join(lines[:3]),
    }
    for name, text in edits.items():
        assert text != original, name
        world.out.write_text(text, "utf-8")
        assert world.check() == 1, name


def test_a_rewritten_tail_passes_alone_and_fails_against_the_anchors(world: World) -> None:
    """The limit of a chain, shown with the real code: only the anchors catch a full rewrite."""
    world.anchor()
    assert world.export() == 0
    records = [json.loads(line) for line in world.out.read_text("utf-8").splitlines()]
    from nuramesh.core.canonical import canonical_json, sha256_hex

    run = [r for r in records if r["stream"] == "run:r1"]
    run[1]["payload"]["n"] = 99
    for i in range(1, len(run)):
        run[i]["prev_hash"] = run[i - 1]["hash"] if i > 1 else run[0]["hash"]
        run[i]["hash"] = sha256_hex(
            canonical_json({k: v for k, v in run[i].items() if k != "hash"})
        )
    rest = [r for r in records if r["stream"] != "run:r1"]
    world.out.write_text(
        "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in run + rest), "utf-8"
    )
    assert verifier.main([str(world.out)]) == 0
    assert world.check() == 1


def test_export_refuses_to_overwrite_and_needs_an_existing_store(
    world: World, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert world.export() == 0
    before = world.out.read_text("utf-8")
    assert world.export() == 2
    assert "already exists" in capsys.readouterr().err
    assert world.out.read_text("utf-8") == before
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["ledger", "export", "--out", str(empty / "x.jsonl"), "--team", str(empty)]) == 2
    assert not (empty / "x.jsonl").exists()
