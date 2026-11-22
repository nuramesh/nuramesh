"""Verify the anchors repository on its remote (R1.30 PR 5, H-13, anchor-remote-rotation spec).

**Why:** the exporter pushes signed heads to a private remote; the remote's history is a second
chain only while nobody rewrites it. A verifier that simply re-reads "the latest" would accept a
rewritten branch. So the verifier keeps a clone of its own, remembers the last tip it verified
without problems (the *pin*, `refs/nuramesh/verified`) and accepts a new tip only if the pin is
an ancestor of it. Anything else is `anchor_history_rewritten`: tampering, never "unanchored".

**How:** a bare repository in a 0700 directory the verifier owns, one lock for the whole run, the
remote's branch fetched into a scratch ref (deleted first, so git never has to judge a
non-fast-forward update), the ancestor test, then the ordinary stream checks of `verify_anchors`
on the clone. The pin moves only after a run with no problem, by compare-and-swap. The clone is
never repaired: after a rewrite it and the pin stay as they were, so the next run says the same.
Only `git ls-remote` and one plain fetch talk to the remote, over ssh with the deploy key from the
environment; nothing is written to the remote, the store or the anchor repository.
"""

import fcntl
import hashlib
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from nuramesh.core.anchors import AnchorProblem
from nuramesh.engine.anchors import (
    _SSH_URL,
    DEFAULT_REF,
    GIT_TIMEOUT,
    PROTECTED_BRANCHES,
    PUSH_TIMEOUT,
    AnchorError,
    AnchorVerification,
    StreamAnchorReport,
    _check_ssh_key,
    _git,
    _remote_url,
    verify_anchor_repository,
)
from nuramesh.stores.base import Store

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PIN_REF = "refs/nuramesh/verified"
INCOMING_REF = "refs/nuramesh/incoming"
LOCK_NAME = "nuramesh-verify.lock"
REWRITTEN = "anchor_history_rewritten"
_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REF_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$")
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class RemoteUnavailable(AnchorError):
    """The remote could not be read (unreachable, timeout, auth); a retry may work."""


@dataclass
class RemoteVerification(AnchorVerification):
    """The result of `verify_remote`: the whole anchor-repository check (streams, repository
    problems, key chain) plus the fetched tip and whether the pin moved."""

    streams: list[StreamAnchorReport] = field(default_factory=list)
    tip: str | None = None  # the fetched tip (what the checks ran against)
    pinned: bool = False  # the pin moved to `tip` in this run


def default_clone_dir(address: str) -> Path:
    """A per-user cache directory named by the SHA-256 of the remote address."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    digest = hashlib.sha256(address.encode()).hexdigest()[:32]
    return Path(base) / "nuramesh" / "anchor-clones" / digest


def remote_address(remote: str, repo: Path | None = None, *, _allow_local: bool = False) -> str:
    """The one address that may be fetched from: an SSH address, or the SSH address of a configured
    remote of `repo`. Nothing else (no http, no `file://`, no path, no option-shaped text) unless
    the engine-only test seam is on. Raises `AnchorError`."""
    if _allow_local and (remote.startswith("file://") or remote.startswith("/")):
        return remote
    if _SSH_URL.match(remote):
        return remote
    if _REMOTE_NAME.match(remote):
        if repo is None:
            raise AnchorError("a remote name needs --repo (the checkout that configures it)")
        return _remote_url(repo, remote)
    raise AnchorError(
        "--remote must be a remote name of --repo or an SSH address (git@host:o/r.git)"
    )


def _check_clone_dir(path: Path, repo: Path | None) -> None:
    if repo is not None:
        root, target = repo.resolve(), path.resolve()
        if target == root or root in target.parents:
            raise AnchorError("the clone directory must not be inside the anchor repository")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        raise AnchorError(f"{path} isn't a directory (the clone directory)")
    if os.name == "posix" and info.st_uid != os.geteuid():
        raise AnchorError(f"{path} is owned by another user; refusing to use it as the clone")
    if os.name == "posix" and info.st_mode & 0o077:
        raise AnchorError(f"{path} must be mode 0700 (others can use it); run: chmod 700 {path}")


def _lock(path: Path, timeout: float) -> int:
    descriptor = os.open(path / LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(descriptor)
                raise AnchorError(
                    "another verification of this clone is running; try again shortly"
                ) from None
            time.sleep(0.05)


def _init_bare(path: Path, ref: str) -> None:
    if (path / "HEAD").is_file():
        return
    if any(entry.name != LOCK_NAME for entry in path.iterdir()):
        raise AnchorError(f"{path} isn't empty and isn't a clone made by nuramesh")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null")
    done = subprocess.run(  # noqa: S603 (an argument list, no shell)
        ["git", "init", "--bare", "-q", f"--initial-branch={ref}", str(path)],
        env=env,
        capture_output=True,
        timeout=GIT_TIMEOUT,
        check=False,
    )
    if done.returncode != 0:
        raise AnchorError("git init failed for the clone directory")
    _git(path, "symbolic-ref", "HEAD", f"refs/heads/{ref}")


def _ref_value(clone: Path, ref: str) -> str | None:
    out = _git(clone, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
    return out.decode().strip() or None


def _has_object(clone: Path, oid: str) -> bool:
    out = _git(clone, "cat-file", "--batch-check", stdin=f"{oid}\n".encode()).decode().split()
    return len(out) >= 2 and out[1] == "commit"


def _is_ancestor(clone: Path, older: str, newer: str) -> bool:
    """`older` is `newer` or reachable from it (nothing of `older` lies outside `newer`)."""
    return _git(clone, "rev-list", "--max-count=1", older, "--not", newer).strip() == b""


def _rewritten(pinned: str | None, fetched: str | None, why: str) -> RemoteVerification:
    detail = f"{why}; pinned tip {pinned or 'none'}, fetched tip {fetched or 'none'}"
    return RemoteVerification(repository_problems=[AnchorProblem(REWRITTEN, detail)], tip=fetched)


def verify_remote(
    store: Store,
    remote: str,
    public_key: "Ed25519PublicKey | Sequence[Ed25519PublicKey]",
    *,
    repo: Path | None = None,
    clone_dir: Path | None = None,
    ref: str = DEFAULT_REF,
    expect_tip: str | None = None,
    streams: list[str] | None = None,
    tail_valid_until: str | None = None,
    ssh_key: Path | None = None,
    lock_timeout: float = GIT_TIMEOUT,
    _allow_local: bool = False,
    _before_pin: Callable[[], None] | None = None,
) -> RemoteVerification:
    """Fetch the anchors branch into the verifier's own clone and check it (module docstring).

    `remote` is a remote name of `repo` or an SSH address (`ssh_key`, the deploy key, is then
    required). A rewritten, shortened, replaced or deleted branch is returned as the repository
    problem `anchor_history_rewritten` and nothing else; a remote that can't be read, a missing
    branch on first use or a clone directory that isn't safe raise `AnchorError`. The two
    underscore parameters are for tests: `_allow_local` accepts a local path or `file://` and no
    CLI option, environment variable or server setting can set it.
    """
    if not _REF_NAME.match(ref) or ref.rsplit("/", 1)[-1] in PROTECTED_BRANCHES:
        raise AnchorError(f"'{ref}' can't be the anchor branch")
    if expect_tip is not None and not _OBJECT_ID.match(expect_tip):
        raise AnchorError("--expect-tip needs a full commit id (40 or 64 hex characters)")
    address = remote_address(remote, repo, _allow_local=_allow_local)
    local = _allow_local and not _SSH_URL.match(address)
    if not local and ssh_key is None:
        raise AnchorError("verifying a remote needs the deploy key: set NURAMESH_ANCHOR_SSH_KEY")
    target = clone_dir if clone_dir is not None else default_clone_dir(address)
    _check_clone_dir(target, repo)
    key = None if local else _check_ssh_key(ssh_key, target) if ssh_key else None
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.mkdir(mode=0o700)
        target.chmod(0o700)
    keys: list[Ed25519PublicKey] = (
        list(public_key) if isinstance(public_key, Sequence) else [public_key]
    )
    descriptor = _lock(target, lock_timeout)
    try:
        return _run(
            store,
            address,
            keys,
            target,
            ref,
            expect_tip,
            streams,
            key,
            local,
            _before_pin,
            tail_valid_until,
        )
    finally:
        os.close(descriptor)  # closing releases the lock


def _run(
    store: Store,
    address: str,
    public_keys: "Sequence[Ed25519PublicKey]",
    clone: Path,
    ref: str,
    expect_tip: str | None,
    streams: list[str] | None,
    key: Path | None,
    local: bool,
    before_pin: Callable[[], None] | None,
    tail_valid_until: str | None,
) -> RemoteVerification:
    _init_bare(clone, ref)
    branch = f"refs/heads/{ref}"
    pin = _ref_value(clone, PIN_REF)
    baseline = pin or _ref_value(clone, branch)
    try:
        listed = _git(
            clone,
            "ls-remote",
            "--heads",
            "--",
            address,
            branch,
            ssh_key=key,
            timeout=PUSH_TIMEOUT,
            allow_local=local,
        ).decode("utf-8", "replace")
    except AnchorError as exc:
        raise RemoteUnavailable(f"could not check the remote: {exc}") from None
    if not any(line.split("\t")[-1:] == [branch] for line in listed.splitlines()):
        if pin is not None:
            return _rewritten(pin, None, "the branch no longer exists on the remote")
        raise AnchorError("no anchors found on the remote (the branch doesn't exist there)")
    _git(clone, "update-ref", "-d", INCOMING_REF)
    try:
        _git(
            clone,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--no-recurse-submodules",
            "--",
            address,
            f"{branch}:{INCOMING_REF}",
            ssh_key=key,
            timeout=PUSH_TIMEOUT,
            allow_local=local,
        )
    except AnchorError as exc:
        raise RemoteUnavailable(f"could not check the remote: {exc}") from None
    fetched = _ref_value(clone, INCOMING_REF)
    if fetched is None:
        raise RemoteUnavailable("could not check the remote: the fetch brought no commit")
    if expect_tip is not None and not (
        _has_object(clone, expect_tip) and _is_ancestor(clone, expect_tip, fetched)
    ):
        return _rewritten(pin, fetched, "the expected tip is not in the remote's history")
    if baseline is not None and not _is_ancestor(clone, baseline, fetched):
        return _rewritten(baseline, fetched, "the remote's history was rewritten")
    old_branch = _ref_value(clone, branch)
    _git(clone, "update-ref", branch, fetched, old_branch or "")  # forward, compare-and-swap
    checked = verify_anchor_repository(
        store, clone, public_keys, streams=streams, ref=ref, tail_valid_until=tail_valid_until
    )
    result = RemoteVerification(
        streams=checked.streams,
        repository_problems=checked.repository_problems,
        chain=checked.chain,
        extra_keys=checked.extra_keys,
        notes=checked.notes,
        tip=fetched,
    )
    if not result.ok:
        return result
    if before_pin is not None:
        before_pin()
    try:
        _git(clone, "update-ref", PIN_REF, fetched, pin or "")
    except AnchorError:
        raise AnchorError("another verification moved the pin, run again") from None
    result.pinned = True
    return result
