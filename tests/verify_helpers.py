"""Load the standalone verifier file (it is not an installed package) and run it on a case."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "verifier" / "nuramesh_verify.py"
CASES = ROOT / "spec" / "verify" / "cases.json"
HAS_GIT = shutil.which("git") is not None
NEEDS_GIT_REASON = "the git binary is not installed (git mode needs it)"
# Obviously fake identity; no user or system configuration is read.
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "test-author@example.invalid",
    "GIT_COMMITTER_NAME": "Test Author",
    "GIT_COMMITTER_EMAIL": "test-author@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def run_git(folder: Path, *args: str) -> str:
    """Run git in a throwaway repository folder (tests only)."""
    done = subprocess.run(
        ["git", *args], cwd=folder, env=GIT_ENV, capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def commit_files(folder: Path, files: dict[str, str | None], message: str = "anchors") -> None:
    """Apply a change set (text, or None to delete) to a repository folder and commit it."""
    for name, content in files.items():
        if content is None:
            (folder / name).unlink()
        else:
            (folder / name).write_bytes(content.encode("utf-8"))
    run_git(folder, "add", "-A")
    run_git(folder, "commit", "-q", "--allow-empty", "-m", message)


def make_repo(folder: Path, branch: str, commits: list[dict[str, str | None]]) -> None:
    """A new repository on `branch`, one commit per change set."""
    folder.mkdir()
    run_git(folder, "init", "-q", "-b", branch)
    run_git(folder, "config", "commit.gpgsign", "false")
    for number, files in enumerate(commits, 1):
        commit_files(folder, files, f"anchors {number}")


def load_verifier() -> ModuleType:
    spec = importlib.util.spec_from_file_location("nuramesh_verify", VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["nuramesh_verify"] = module  # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


def load_cases() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(CASES.read_text(encoding="utf-8"))
    return data


def build_repo(git: dict[str, Any], folder: Path) -> None:
    """Make `folder` the case's repository: `commits`, then `steps` (git arguments), then the
    `worktree` files. A `layout` makes it a shallow clone, a `.git` file or a bare repository."""
    layout = git.get("layout")
    scratch = folder.parent / "scratch"
    work = scratch / "work" if layout else folder
    if layout:
        scratch.mkdir()
    make_repo(work, git["branch"], git["commits"])
    for step in git.get("steps", []):
        run_git(work, *step)
    if layout == "shallow":
        run_git(scratch, "clone", "-q", "--depth", "1", f"file://{work}", str(folder))
    elif layout == "gitfile":  # `.git` is a file pointing at the real git directory
        shutil.move(str(work / ".git"), str(scratch / "gitdir"))
        (work / ".git").write_text(f"gitdir: {scratch / 'gitdir'}\n", encoding="utf-8")
        shutil.move(str(work), str(folder))
    elif layout == "bare":  # the git directory's own content is the directory's content
        for entry in work.iterdir():
            if entry.name != ".git":
                entry.unlink()
        for entry in list((work / ".git").iterdir()):
            shutil.move(str(entry), str(work / entry.name))
        (work / ".git").rmdir()
        shutil.move(str(work), str(folder))
    for name, content in git.get("worktree", {}).items():
        if content is None:
            (folder / name).unlink()
        else:
            (folder / name).write_bytes(content.encode("utf-8"))


def write_case(case: dict[str, Any], keys: dict[str, str], root: Path) -> list[str]:
    """Write a case's files under `root`; return the command-line arguments that check it."""
    ledger = root / "ledger.jsonl"
    if "ledger_hex" in case:
        ledger.write_bytes(bytes.fromhex(case["ledger_hex"]))
    else:
        ledger.write_bytes(case["ledger"].encode("utf-8"))
    args = [str(ledger)]
    if case["anchors"] is not None:
        folder = root / "anchors"
        if "git" in case:
            build_repo(case["git"], folder)
        else:
            folder.mkdir()
            for name, content in case["anchors"].items():
                (folder / name).write_bytes(content.encode("utf-8"))
        args += ["--anchors", str(folder)]
        for name in case["keys"]:
            key = root / f"{name}.pem"
            key.write_text(keys[name], encoding="utf-8")
            args += ["--pubkey", str(key)]
    if case["options"].get("git"):
        args.append("--git")
    ref = case["options"].get("git_ref")
    if ref:
        if "{head" in ref:  # a placeholder for a real commit id of the case's repository
            folder = root / "anchors"
            for placeholder, revision in (("{head~1}", "HEAD~1"), ("{head}", "HEAD")):
                if placeholder in ref:
                    ref = ref.replace(placeholder, run_git(folder, "rev-parse", revision))
        args += ["--git-ref", ref]
    commit = case["options"].get("git_commit")
    if commit:
        if commit == "{HEAD}":  # the real tip id in upper case
            commit = run_git(root / "anchors", "rev-parse", "HEAD").upper()
        elif commit == "{head}":
            commit = run_git(root / "anchors", "rev-parse", "HEAD")
        args += ["--git-commit", commit]
    if case["options"]["require_anchors"]:
        args.append("--require-anchors")
    for stream, head in case["options"]["expected_heads"].items():
        args += ["--expected-head", f"{stream}={head}"]
    return args
