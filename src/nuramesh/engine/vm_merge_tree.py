"""The `mt-1` pinned merge-tree runner (section 4 "Pinned recomputation",
GT-15 b, SV-8 d, VF-3.9).

Every `git merge-tree` the gate, the signer and the verifier run goes through this class so that
the three agree: a bare scratch repository, an environment built from nothing
(`GIT_CONFIG_NOSYSTEM=1`, `GIT_CONFIG_GLOBAL=/dev/null`, `HOME` a new empty directory for each
run, no system attributes; nothing of the caller's environment but `PATH`), and **exactly the
five `-c` values of `PIN_CONFIG`, no other `-c`**. No user or system attributes file can exist
for the run (the empty `HOME`, no `XDG_CONFIG_HOME`), and an `info/attributes` file inside the
scratch repository is refused. The scratch repository's own `config` is refused unless it holds only
`SCRATCH_CONFIG_ALLOWLIST` (what `git init --bare` writes; no `include`, `core.attributesFile`,
`merge.*` or `core.ignorecase=true`), and a `commondir` file (it redirects the config) is refused
too (`scratch_config`). The pinned git is **git >= 2.40** (`--attr-source`); an older git fails
every run (`git_failed`) instead of silently disabling attributes. Attributes are read
**from the base tip only**
(`git --attr-source=<base tip>`, git >= 2.40), never from either side being merged, so a
`merge=union` or `-merge` line committed in a PR tree cannot change the result; without a base
tip the empty tree is the source, which disables attributes (section 4's fallback). A test proves
both against the installed git. A change to any pinned value is a new pin id, never a silent
change of `mt-1`.

Arguments are 40-hex commit ids only (never a ref, an option or an abbreviation), git runs as an
argument list with no shell, hooks, replace refs, lazy fetches and prompts off, output bounded, and
a hung git is killed with its process group. The runner never raises for the two expected answers
(a tree, or None for a conflict); anything else is `MergeTreeError` with a code and no git output in
the message. The pure graph rule over the results lives in `nuramesh.core.vm_carry`.
"""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

PIN = "mt-1"
PIN_CONFIG: tuple[str, ...] = (
    "merge.renames=true",
    "merge.directoryRenames=conflict",
    "diff.renameLimit=1000",
    "merge.renormalize=false",
    "core.autocrlf=false",
)
"""Section 4, in the spec's order. Frozen: a change is a new pin id."""

_SHA = re.compile(r"^[0-9a-f]{40}$")
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
"""git's empty tree: as `--attr-source` it disables every in-tree attribute."""
MAX_OUTPUT_BYTES = 1 << 20
_FALSE = ("false", "no", "off", "0")
SCRATCH_CONFIG_ALLOWLIST: frozenset[str] = frozenset(
    {
        "core.repositoryformatversion",
        "core.filemode",
        "core.bare",
        "core.logallrefupdates",
        "core.symlinks",
        "extensions.objectformat",
    }
)
"""The only keys the scratch repository's own config may hold (`core.ignorecase` only as false)."""


class MergeTreeError(Exception):
    """git could not answer: `git_missing`, `no_merge_base` (merge-base: the commits share no
    history), `git_failed` (a non-0/1 exit: an unknown object, a corrupt repository), `timeout`,
    `bad_output`, `scratch_attributes` (the scratch repository holds an `info/attributes` file),
    `scratch_config` (its `config` holds anything outside `SCRATCH_CONFIG_ALLOWLIST`, is
    unparsable, or the repository has a `commondir` file).
    The message is the code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"merge-tree: {code}")


def git_family(version: str) -> str:
    """`major.minor` of a git version string (the family the pin is recorded for)."""
    parts = version.strip().split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts[:2]):
        raise ValueError("not a git version")
    return f"{parts[0]}.{parts[1]}"


def _sha(value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ValueError("merge-tree arguments are 40-hex commit ids")
    return value


class MergeTreeRunner:
    """Pinned git over one bare scratch repository (`git_dir`)."""

    pin = PIN

    def __init__(
        self,
        git_dir: Path,
        *,
        attr_source: str | None = None,
        timeout_seconds: float = 60.0,
        path: str | None = None,
    ) -> None:
        self.git_dir = Path(git_dir)
        self.path = path
        """The `PATH` that finds the pinned git (the service's configuration); None: the
        caller's `PATH`, the one value taken from the caller's environment."""
        self.attr_source = _sha(attr_source) if attr_source is not None else EMPTY_TREE
        """The commit (the base tip) whose `.gitattributes` apply; the empty tree disables them."""
        self.timeout_seconds = timeout_seconds

    # -- the hermetic command ------------------------------------------------------------------

    def environment(self, home: Path) -> dict[str, str]:
        """The whole environment of one run: built from nothing, `home` an empty directory."""
        path = self.path if self.path is not None else os.environ.get("PATH", "/usr/bin:/bin")
        return {
            "PATH": path,
            "HOME": str(home),
            "GIT_DIR": str(self.git_dir),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "LC_ALL": "C",
        }

    def _argv(self, *args: str) -> list[str]:
        argv = ["git", "--no-replace-objects", f"--attr-source={self.attr_source}"]
        for setting in PIN_CONFIG:  # exactly these five: another `-c` would be another pin
            argv.extend(("-c", setting))
        argv.extend(args)
        return argv

    def _run(self, *args: str) -> tuple[int, bytes]:
        if (self.git_dir / "info" / "attributes").exists():
            raise MergeTreeError("scratch_attributes")
        self._check_scratch_config()
        try:
            with tempfile.TemporaryDirectory(prefix="vm-mt1-home-") as home:
                return self._run_in(Path(home), *args)
        except OSError:
            raise MergeTreeError("git_failed") from None

    def _check_scratch_config(self) -> None:
        """Fail closed unless the scratch repository's own config is the allowlist (a config
        `core.attributesFile` or `merge.<x>.driver` would otherwise reach every run)."""
        if (self.git_dir / "commondir").exists():
            raise MergeTreeError("scratch_config")
        config = self.git_dir / "config"
        if not config.is_file():
            raise MergeTreeError("scratch_config")
        try:
            with tempfile.TemporaryDirectory(prefix="vm-mt1-cfg-") as home:
                env = self.environment(Path(home))
                env.pop("GIT_DIR")
                done = subprocess.run(  # noqa: S603 (an argument list, no shell)
                    ["git", "config", "--file", str(config), "--list", "-z"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    env=env,
                    cwd=home,
                    timeout=self.timeout_seconds,
                    check=False,
                )
        except FileNotFoundError:
            raise MergeTreeError("git_missing") from None
        except (OSError, subprocess.SubprocessError):
            raise MergeTreeError("scratch_config") from None
        if done.returncode != 0 or len(done.stdout) > MAX_OUTPUT_BYTES:
            raise MergeTreeError("scratch_config")
        for entry in done.stdout.split(b"\0"):
            if not entry:
                continue
            key, _, value = entry.decode("utf-8", "replace").partition("\n")
            if key not in SCRATCH_CONFIG_ALLOWLIST:
                if key == "core.ignorecase" and value.strip().lower() in (
                    "false",
                    "no",
                    "off",
                    "0",
                ):
                    continue
                raise MergeTreeError("scratch_config")

    def _run_in(self, home: Path, *args: str) -> tuple[int, bytes]:
        try:
            proc = subprocess.Popen(  # noqa: S603 (an argument list, no shell)
                self._argv(*args),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.environment(home),
                cwd=str(self.git_dir),
                start_new_session=True,
            )
        except FileNotFoundError:
            raise MergeTreeError("git_missing") from None
        except OSError:
            raise MergeTreeError("git_failed") from None
        out = self._read_capped(proc)
        return proc.wait(), out

    def _read_capped(self, proc: subprocess.Popen[bytes]) -> bytes:
        """Read stdout up to MAX_OUTPUT_BYTES while it arrives; kill git on the cap or the
        deadline, so an endless output never sits in memory."""
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        deadline = time.monotonic() + self.timeout_seconds
        chunks: list[bytes] = []
        size = 0
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while True:
                left = deadline - time.monotonic()
                if left <= 0 or not sel.select(left):
                    _kill(proc)
                    raise MergeTreeError("timeout")
                chunk = os.read(fd, 65536)
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > MAX_OUTPUT_BYTES:
                    _kill(proc)
                    raise MergeTreeError("bad_output")
                chunks.append(chunk)

    # -- the pinned questions ------------------------------------------------------------------

    def git_version(self) -> str:
        code, out = self._run("--version")
        if code != 0:
            raise MergeTreeError("git_failed")
        words = out.decode("ascii", "replace").split()
        if len(words) < 3:
            raise MergeTreeError("bad_output")
        return words[2]

    def _write_tree(self, *args: str) -> str | None:
        code, out = self._run("merge-tree", "--write-tree", *args)
        if code not in (0, 1):
            raise MergeTreeError("git_failed")
        first = out.split(b"\n", 1)[0].decode("ascii", "replace").strip()
        if not _SHA.fullmatch(first):
            raise MergeTreeError("bad_output")
        return first if code == 0 else None

    def merge_tree(self, p1: str, p2: str) -> str | None:
        """The tree of the pinned clean merge of two commits, or None on any conflict (GT-15 b)."""
        return self._write_tree(_sha(p1), _sha(p2))

    def merge_tree_with_base(self, merge_base: str, a: str, b: str) -> str | None:
        """Section 4, mechanical rebase: `merge-tree --write-tree --merge-base=<mb0> N H0`."""
        return self._write_tree(f"--merge-base={_sha(merge_base)}", _sha(a), _sha(b))

    def is_ancestor(self, maybe_ancestor: str, descendant: str) -> bool:
        code, _ = self._run("merge-base", "--is-ancestor", _sha(maybe_ancestor), _sha(descendant))
        if code == 0:
            return True
        if code == 1:
            return False
        raise MergeTreeError("git_failed")

    def in_base_history(self, base_tip: str) -> Callable[[str], bool]:
        """`in_base_history(sha)` for `vm_carry.carry_forward`: is the commit in the protected
        branch's history at `base_tip`."""
        tip = _sha(base_tip)
        return lambda commit: self.is_ancestor(commit, tip)

    def merge_bases(self, a: str, b: str) -> tuple[str, ...]:
        """`git merge-base --all`; more than one is `bundle_invalid merge_base_ambiguous`
        upstream (section 4)."""
        code, out = self._run("merge-base", "--all", _sha(a), _sha(b))
        if code == 1:  # git merge-base: exit 1 is "no common ancestor"
            raise MergeTreeError("no_merge_base")
        if code != 0:
            raise MergeTreeError("git_failed")
        found = tuple(line.decode("ascii", "replace").strip() for line in out.splitlines())
        if not found or any(not _SHA.fullmatch(item) for item in found):
            raise MergeTreeError("bad_output")
        return tuple(sorted(found))

    def read_commit(self, commit: str) -> tuple[tuple[str, ...], str] | None:
        """`read_commit(sha)` for `vm_carry.carry_forward` (GT-15): git's own `(parents, tree)`
        of a commit, read from the commit object's header, or None when the repository has no
        such commit (fail closed upstream)."""
        code, out = self._run("cat-file", "commit", _sha(commit))
        if code != 0:
            return None
        parents: list[str] = []
        trees: list[str] = []
        for line in out.split(b"\n\n", 1)[0].split(b"\n"):
            key, _, value = line.partition(b" ")
            text = value.decode("ascii", "replace")
            if key == b"tree":
                trees.append(text)
            elif key == b"parent":
                parents.append(text)
        if len(trees) != 1 or any(not _SHA.fullmatch(item) for item in (*trees, *parents)):
            raise MergeTreeError("bad_output")
        return tuple(parents), trees[0]

    def tree_of(self, commit: str) -> str:
        code, out = self._run("rev-parse", "--verify", "--end-of-options", _sha(commit) + "^{tree}")
        if code != 0:
            raise MergeTreeError("git_failed")
        value = out.decode("ascii", "replace").strip()
        if not _SHA.fullmatch(value):
            raise MergeTreeError("bad_output")
        return value


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill git and anything it started (its own process group); never another PID."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - a zombie is the OS's problem now
        pass
