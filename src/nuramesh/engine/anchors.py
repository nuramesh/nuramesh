"""Export ledger heads to a Git repository and check them later (R1.04a, ND22).

**Why:** a hash chain only proves integrity against a head kept outside the writer's database.
Anyone who can edit the database could recompute the chain; they can't rewrite signed heads that
already live in a Git repository they don't control, whose history is a second chain.

**How:** `export_heads` appends one signed line per stream to `<anchor repo>/<stream>.jsonl` (see
`core.anchors`) when the stream has events newer than its last anchor, then makes one Git commit
with **plumbing commands only** (`hash-object --no-filters`, a temporary index, `write-tree`,
`commit-tree`, and a compare-and-swap `update-ref`): no `git add` and no `git status`, so
repository filters, hooks, fsmonitor and `.gitignore` can't run code or swallow an anchor, and a
failed or racing export changes nothing.
Signatures are Ed25519 (`nuramesh[anchors]`, the `cryptography` package). The **private key is a
file outside the database and outside the anchor repository** (create it with `generate_key`; mode
0600); the **public key** is what verifiers keep (`verify_anchors` needs it, never the private
key). The repository must be a checkout on a branch that isn't `main`/`master` (anchors never go
into the product repository's main), and it should be as private as the product repository
(ND21). Git runs hardened like canon loading: explicit `GIT_DIR`, no user or system config, no
hooks, no fsmonitor, **replace refs ignored**, no pager, no prompts, no network.

**Verification** (`verify_anchors`), per stream: every signature must verify against the given
public key; anchors must match the stream's events (`core.anchors.check_anchors`: nothing at or
before an anchor changed or was removed); and the Git history of the anchor file must only ever
**append** (each committed version is a prefix of the next), so rewriting an anchor shows up even
if someone force-pushes. A stream with no anchors is reported as `unanchored`; whether that is a
warning or a failure is the caller's policy (`require_anchors`). Pushing the anchor repository to a
remote is left to the operator (`git push` in a cron job); a local-only anchor repository protects
against database edits, not against someone who can also edit that repository.

Anchoring never reads or writes ledger content: only stream id, sequence number, head hash and
time are exported.
"""

import contextlib
import fcntl
import hashlib
import os
import re
import shlex
import signal
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from nuramesh.core.anchors import (
    KEY_FILE,
    Anchor,
    AnchorFileError,
    AnchorProblem,
    ChainKey,
    KeyChain,
    KeyChange,
    Retirement,
    anchor_filename,
    anchor_message,
    anchor_time,
    check_anchors,
    check_key_rules,
    key_change_message,
    key_rotated_payload,
    parse_anchor_lines,
    unanchored_events,
    walk_key_chain,
)
from nuramesh.core.canonical import UnsafeNumberError
from nuramesh.core.ledger import EventType, LedgerEvent
from nuramesh.stores.base import EventDraft, Store

try:  # the `anchors` extra
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
except ImportError:  # pragma: no cover
    Ed25519PrivateKey = None  # type: ignore[assignment, misc]

GIT_TIMEOUT = 60
DEFAULT_REF = "ledger-anchors"
PROTECTED_BRANCHES = frozenset({"main", "master"})
PUSH_TIMEOUT = 120
ANCHOR_EMAIL = "anchors@nuramesh.invalid"  # the exporter's identity on every commit
SSH_KEY_ENV = "NURAMESH_ANCHOR_SSH_KEY"
_SSH_URL = re.compile(
    r"^(ssh://[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9.-]+(:[0-9]{1,5})?/"
    r"|[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9.-]+:)"
    r"[A-Za-z0-9._/-]+$"
)


class AnchorError(ValueError):
    """Anchoring can't continue (missing extra, bad key, bad repository); nothing was exported."""


def _require_crypto() -> None:
    if Ed25519PrivateKey is None:  # pragma: no cover
        raise AnchorError("ledger anchoring needs the extra: pip install 'nuramesh[anchors]'")


def key_id(public_key: Ed25519PublicKey) -> str:
    """A short, stable id for a public key (16 hex characters of its SHA-256)."""
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


def generate_key(private_path: Path, public_path: Path) -> str:
    """Create a key pair: the private key (mode 0600, **keep it outside Git and the database**) and
    its public half. Refuses to overwrite. Returns the key id."""
    _require_crypto()
    for path in (private_path, public_path):
        if path.exists():
            raise AnchorError(f"{path} already exists; refusing to overwrite a key")
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    descriptor = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(pem)
        public_path.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
    except OSError:
        private_path.unlink(missing_ok=True)  # never leave a private key without its public half
        raise
    return key_id(key.public_key())


def load_private_key(path: Path) -> Ed25519PrivateKey:
    """Read a private key (PEM); refuses a key file that others can read."""
    _require_crypto()
    # opened without following a link and checked on the open descriptor, so a swap after a check
    # can't redirect the read (R1.30 review-2 m5)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        raise AnchorError(f"{path} isn't a usable Ed25519 private key") from None
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise AnchorError(f"{path} isn't a usable Ed25519 private key")
        if os.name == "posix" and info.st_uid != os.geteuid():
            raise AnchorError(f"{path} is not owned by the current user")
        if os.name == "posix" and info.st_mode & 0o077:
            raise AnchorError(f"{path} is readable by others; run: chmod 600 {path}")
        data = handle.read(65536)
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        raise AnchorError(f"{path} isn't a usable Ed25519 private key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise AnchorError(f"{path} isn't an Ed25519 key")
    return key


def load_public_key(path: Path) -> Ed25519PublicKey:
    """Read a public key (PEM)."""
    _require_crypto()
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except (ValueError, TypeError, OSError):
        raise AnchorError(f"{path} isn't a usable Ed25519 public key") from None
    if not isinstance(key, Ed25519PublicKey):
        raise AnchorError(f"{path} isn't an Ed25519 key")
    return key


_FILE = re.compile(r"^(run|org)__[a-z0-9][a-z0-9_-]{0,62}\.jsonl$")
MAX_FILE_BYTES = 5_000_000


def _git_dir(repo: Path) -> Path:
    if (repo / ".git").is_dir() and not (repo / ".git").is_symlink():
        return repo / ".git"
    if (repo / "HEAD").is_file() and (repo / "objects").is_dir():  # a bare repository
        return repo
    raise AnchorError(f"{repo} isn't a git repository root")


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
    stdin: bytes | None = None,
    env_extra: dict[str, str] | None = None,
    ssh_key: Path | None = None,
    timeout: int = GIT_TIMEOUT,
    allow_local: bool = False,
) -> bytes:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_DIR": str(_git_dir(repo)),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": "/dev/null",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PAGER": "cat",
        "GIT_AUTHOR_NAME": "NuraMesh anchors",
        "GIT_AUTHOR_EMAIL": ANCHOR_EMAIL,
        "GIT_COMMITTER_NAME": "NuraMesh anchors",
        "GIT_COMMITTER_EMAIL": ANCHOR_EMAIL,
        "LC_ALL": "C",
        **(env_extra or {}),
    }
    network: list[str] = []
    if ssh_key is not None:  # the one call that may talk to a remote: ssh only, this key only
        env["GIT_ALLOW_PROTOCOL"] = "ssh"
        env["GIT_SSH_COMMAND"] = " ".join(
            shlex.quote(part)
            for part in (
                "ssh",
                "-F",
                "/dev/null",
                "-i",
                str(ssh_key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
            )
        )
        network = ["-c", "protocol.ssh.allow=always"]
    if allow_local:  # tests only, through verify_remote's engine-only seam (R8)
        env["GIT_ALLOW_PROTOCOL"] = "file"
        network = ["-c", "protocol.file.allow=always"]
    command = [
        "git",
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "commit.gpgsign=false",
        # a repository's own config is untrusted: no signature display or push signing, and no
        # program named by it (gpg, ssh, fsmonitor, pager) may run (R1.30 review 3, M1)
        "-c",
        "log.showSignature=false",
        "-c",
        "push.gpgSign=false",
        "-c",
        "gpg.program=/bin/false",
        "-c",
        "gpg.ssh.program=/bin/false",
        "-c",
        "gpg.x509.program=/bin/false",
        "-c",
        "core.sshCommand=",
        "-c",
        "core.pager=cat",
        "-c",
        "protocol.allow=never",
        *network,
        *args,
    ]
    try:
        process = subprocess.Popen(  # noqa: S603 (an argument list, no shell)
            command,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,  # so a timeout can stop git's children (ssh) too
        )
    except FileNotFoundError:
        raise AnchorError("git isn't installed") from None
    try:
        out, _err = process.communicate(input=stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise AnchorError(f"git {args[0]} took longer than {timeout}s") from None
    except BaseException:  # Ctrl-C included: the new session means the terminal won't reach git
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise
    done = subprocess.CompletedProcess(command, process.returncode, out)
    if check and done.returncode != 0:
        raise AnchorError(f"git {args[0]} failed in the anchor repository")
    return done.stdout


def _branch(repo: Path) -> str:
    done = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).decode().strip()
    if not done:
        raise AnchorError("the anchor repository isn't on a branch (detached HEAD): check one out")
    return done


def _branch_or_none(repo: Path) -> str | None:
    out = _git(repo, "symbolic-ref", "--short", "-q", "HEAD", check=False).decode().strip()
    return out or None


def _refuse_shallow(repo: Path) -> None:
    shallow = _git(repo, "rev-parse", "--is-shallow-repository").decode().strip() == "true"
    if shallow or (_git_dir(repo) / "shallow").exists():
        raise AnchorError("the anchor repository is a shallow clone: its history can't be checked")


def _head_commit(repo: Path) -> str | None:
    """The commit HEAD points at; None only for a repository with no commits yet.

    Any other failure raises: a verifier must never read "git failed" as "nothing to check".
    """
    _refuse_shallow(repo)
    unborn = _git(repo, "for-each-ref", "--count=1", "refs/heads").decode().strip() == ""
    if unborn:
        return None
    return _git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()


def _ref_commit(repo: Path, ref: str) -> str:
    """The commit a named ref points at; refuses main/master, shallow clones and a missing ref."""
    _refuse_shallow(repo)
    short = ref.rsplit("/", 1)[-1]
    if short in PROTECTED_BRANCHES:
        raise AnchorError(f"'{ref}' is a main branch: anchors never live there")
    for candidate in (ref, f"refs/heads/{ref}", f"refs/remotes/{ref}"):
        done = _git(repo, "rev-parse", "--verify", "-q", f"{candidate}^{{commit}}", check=False)
        if done.strip():
            return done.decode().strip()
    raise AnchorError(f"the anchor ref '{ref}' wasn't found in the repository")


def _committed_file(repo: Path, commit: str | None, filename: str) -> bytes | None:
    """The file as committed at `commit` (None if absent); refuses links and huge files."""
    if commit is None:
        return None
    listing = _git(repo, "ls-tree", "-l", commit, "--", filename).decode().split()
    if not listing:
        return None
    mode, size = listing[0], listing[3]
    if mode != "100644":
        raise AnchorError(f"{filename} isn't a regular file in the anchor repository")
    if size.isdigit() and int(size) > MAX_FILE_BYTES:
        raise AnchorError(f"{filename} is larger than {MAX_FILE_BYTES} bytes")
    return _git(repo, "cat-file", "blob", f"{commit}:{filename}")


def read_anchor_file(repo: Path, stream: str, commit: str | None = None) -> list[Anchor]:
    """The stream's anchors **as committed** at `commit` (default `HEAD`); empty if none."""
    root = repo
    at = commit or _head_commit(root)
    name = anchor_filename(stream)
    data = _committed_file(root, at, name)
    if data is None:
        return []
    try:
        return parse_anchor_lines(data.decode("utf-8"), stream=stream)
    except (UnicodeDecodeError, AnchorFileError) as exc:
        raise AnchorFileError(f"{name}: {exc}") from None


def _working_copy_differs(repo: Path, commit: str | None, filename: str) -> bool:
    """True if the anchor file in the working tree isn't exactly what is committed."""
    if (repo / ".git").is_dir() is False:  # a bare repository has no working tree
        return False
    path = repo / filename
    committed = _committed_file(repo, commit, filename)
    if commit is not None and _branch_or_none(repo) is None:
        return False  # a detached checkout has no branch whose files it must mirror
    if not path.exists() and not path.is_symlink():
        return committed is not None
    if path.is_symlink() or not path.is_file():
        return True
    return committed is None or path.read_bytes().replace(b"\r\n", b"\n") != committed


def preflight_repository(repo: Path) -> None:
    """Checks an export needs, without exporting: a repository root on a named branch that isn't
    main/master, not shallow, with a readable history. Raises `AnchorError`."""
    _git_dir(repo)
    if _branch(repo) in PROTECTED_BRANCHES:
        raise AnchorError(
            "anchors never go on main/master: check out a branch such as ledger-anchors"
        )
    _head_commit(repo)


@dataclass
class ExportReport:
    """What an export did."""

    exported: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    commit: str | None = None


def _commit_files(root: Path, parent: str | None, new_files: dict[str, bytes], message: str) -> str:
    """Commit `new_files` (name -> full new content) on the checked-out branch with plumbing only
    and bring the working tree and index in line. Returns the commit id; a failed sync rolls the
    commit back."""
    with tempfile.TemporaryDirectory(prefix="nuramesh-index-") as scratch:
        env = {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        if parent is not None:
            _git(root, "read-tree", parent, env_extra=env)
        for name, content in new_files.items():
            blob = (
                _git(
                    root,
                    "hash-object",
                    "-w",
                    "--no-filters",
                    "--stdin",
                    stdin=content,
                    env_extra=env,
                )
                .decode()
                .strip()
            )
            _git(
                root, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", env_extra=env
            )
        tree = _git(root, "write-tree", env_extra=env).decode().strip()
    commit_args = ["commit-tree", tree, "-m", message]
    if parent is not None:
        commit_args += ["-p", parent]
    commit = _git(root, *commit_args).decode().strip()
    branch = _branch(root)
    ref = f"refs/heads/{branch}"
    # an empty old value means "the ref must not exist yet" (any hash length); otherwise the old
    # commit: the update fails if HEAD moved meanwhile, so racing exports can't both win
    _git(root, "update-ref", ref, commit, parent or "")
    if (root / ".git").is_dir():  # bring the working tree and index in line with the commit
        previous = {n: _committed_file(root, parent, n) for n in new_files}
        try:
            for name, content in new_files.items():
                written = _git(root, "hash-object", "-w", "--no-filters", "--stdin", stdin=content)
                _git(
                    root,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"100644,{written.decode().strip()},{name}",
                )  # only our files: whatever else the operator staged stays staged
                (root / name).write_bytes(content)
        except (AnchorError, OSError) as exc:
            # a failed sync (for example a held index.lock) must not leave the commit behind
            _git(root, "update-ref", "-d", ref, commit) if parent is None else _git(
                root, "update-ref", ref, parent, commit
            )
            for name, old in previous.items():
                if old is None:
                    (root / name).unlink(missing_ok=True)
                else:
                    (root / name).write_bytes(old)
            raise AnchorError(
                "the anchor commit was rolled back because the working tree couldn't be updated"
            ) from exc
    return commit


def export_heads(
    store: Store,
    repo: Path,
    private_key: Ed25519PrivateKey,
    *,
    streams: list[str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> ExportReport:
    """Sign the current head of each stream (default: all) and commit the new anchors.

    A stream whose head is already its last committed anchor is skipped; if nothing is new, no
    commit is made. Refuses a repository on `main`/`master`, a shallow clone, a detached HEAD,
    uncommitted edits to an anchor file, and a stream that no longer matches the anchors it already
    has (signing it would bless a rewritten history: run `verify-ledger`). The CLI also refuses a
    key stored inside the repository.
    """
    _require_crypto()
    root = repo
    _git_dir(root)
    if _branch(root) in PROTECTED_BRANCHES:
        raise AnchorError(
            "anchors never go on main/master: check out a branch such as ledger-anchors"
        )
    parent = _head_commit(root)
    kid = key_id(private_key.public_key())
    for retired in _retired_ids(root, parent):
        if retired[0] == kid:
            raise AnchorError(f"this key was retired on {retired[1]}")
    report = ExportReport()
    new_files: dict[str, bytes] = {}
    for stream in sorted(streams if streams is not None else store.streams()):
        head = store.head(stream)
        if head is None:
            continue
        name = anchor_filename(stream)
        if _working_copy_differs(root, parent, name):
            raise AnchorError(f"{name} has uncommitted changes; commit or restore it first")
        committed = _committed_file(root, parent, name) or b""
        try:
            existing = parse_anchor_lines(committed.decode("utf-8"), stream=stream)
        except (UnicodeDecodeError, AnchorFileError) as exc:
            raise AnchorError(f"{name} can't be read: {exc}") from None
        events, _ = store.snapshot(stream)
        mismatch = check_anchors(stream, existing, events)
        if mismatch:
            raise AnchorError(
                f"stream '{stream}' no longer matches its anchors ({mismatch[0].code}); "
                "refusing to sign it: run verify-ledger"
            )
        if existing and existing[-1].seq == head.seq and existing[-1].head == head.hash:
            report.unchanged.append(stream)
            continue
        at = anchor_time((now or store.now)())
        signature = private_key.sign(anchor_message(stream, head.seq, head.hash, at, kid)).hex()
        anchor = Anchor(
            stream=stream, seq=head.seq, head=head.hash, at=at, key_id=kid, sig=signature
        )
        new_files[name] = committed + (anchor.line() + "\n").encode("utf-8")
        report.exported.append(stream)
    if not new_files:
        return report
    message = f"anchors: {len(new_files)} stream(s) ({', '.join(sorted(report.exported))[:200]})"
    report.commit = _commit_files(root, parent, new_files, message)
    return report


@dataclass
class StreamAnchorReport:
    """The anchor check of one stream."""

    stream: str
    anchors: int
    unanchored_events: int
    problems: list[AnchorProblem] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """No problems (a stream with no anchors at all has none to check: see `anchored`)."""
        return not self.problems

    @property
    def anchored(self) -> bool:
        """The stream has at least one anchor."""
        return self.anchors > 0


def _ever_files(repo: Path, commit: str) -> set[str]:
    """Every anchor-file name present in any commit reachable from HEAD."""
    out = _git(
        repo,
        "-c",
        "log.showRoot=true",
        "log",
        "--no-show-signature",
        "--full-history",
        "-m",
        "--no-renames",
        "--name-only",
        "--format=",
        commit,
    )
    return {line for line in out.decode("utf-8", "replace").splitlines() if _FILE.match(line)}


def _versions_by_commit(
    repo: Path, commit: str, filenames: list[str]
) -> list[tuple[str, dict[str, bytes | None]]]:
    """For every commit reachable from `commit` (oldest first), each named file's content there
    (None if absent): one `cat-file --batch` for everything."""
    commits = _git(repo, "rev-list", "--reverse", commit).decode().split()
    if not commits or not filenames:
        return [(c, {n: None for n in filenames}) for c in commits]
    pairs = [(c, n) for c in commits for n in filenames]
    out = _git(repo, "cat-file", "--batch", stdin="".join(f"{c}:{n}\n" for c, n in pairs).encode())
    result: dict[str, dict[str, bytes | None]] = {c: {} for c in commits}
    position = 0
    for found_commit, name in pairs:
        line_end = out.index(b"\n", position)
        header = out[position:line_end].split()
        position = line_end + 1
        if len(header) == 2 and header[1] == b"missing":
            result[found_commit][name] = None
            continue
        size = int(header[2])
        result[found_commit][name] = out[position : position + size]
        position += size + 1
    return [(c, result[c]) for c in commits]


def _lines(data: bytes | None) -> int:
    return len(data.splitlines()) if data else 0


def _hex_public(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def _derive_id(public_hex: str) -> str | None:
    try:
        raw = bytes.fromhex(public_hex)
        Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError):
        return None
    return hashlib.sha256(raw).hexdigest()[:16]


def _verify_hex(public_hex: str, signature_hex: str, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
            bytes.fromhex(signature_hex), message
        )
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def _root_commits(repo: Path, commit: str) -> set[str]:
    return set(_git(repo, "rev-list", "--max-parents=0", commit).decode().split())


def _retired_ids(repo: Path, commit: str | None) -> list[tuple[str, str]]:
    """(key id, retirement time) of every key a valid-looking record at `commit` retires. Used by
    `export` to refuse a retired key; the full chain check is the verifier's job."""
    if commit is None:
        return []
    data = _committed_file(repo, commit, KEY_FILE)  # an unreadable file refuses (fail closed)
    if not data:
        return []
    chain = walk_key_chain(
        data.decode("utf-8", "replace"),
        root=None,
        repo_roots=_root_commits(repo, commit),
        verify=_verify_hex,
        derive_id=_derive_id,
    )
    return [(r.prev_key_id, r.at) for r in chain.records]


@dataclass
class AnchorVerification:
    """The whole check of an anchor repository (rule R17): per-stream reports, problems that
    belong to the repository itself, the key chain that was followed and notes to print."""

    streams: list[StreamAnchorReport]
    repository_problems: list[AnchorProblem] = field(default_factory=list)
    chain: list[str] = field(default_factory=list)
    extra_keys: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """No repository problem and no stream problem."""
        return not self.repository_problems and all(r.ok for r in self.streams)

    def to_json(self) -> dict[str, object]:
        """The report shape of `anchor verify --json` (the `repository` block of rule R17)."""
        return {
            "repository": {
                "ok": not self.repository_problems,
                "problems": [
                    {"code": p.code, "detail": p.detail} for p in self.repository_problems
                ],
                "chain": self.chain,
                "extra_keys": self.extra_keys,
            },
            "streams": [
                {
                    "stream": r.stream,
                    "anchors": r.anchors,
                    "unanchored_events": r.unanchored_events,
                    "problems": [{"code": p.code, "detail": p.detail} for p in r.problems],
                }
                for r in self.streams
            ],
            "ok": self.ok,
        }


Keyring = dict[str, tuple[int, "Ed25519PublicKey"]]
_Versions = list[tuple[str, dict[str, bytes | None]]]


def _key_chain(
    repo: Path,
    head_commit: str | None,
    public_keys: Sequence[Ed25519PublicKey],
    versions: _Versions,
    result: AnchorVerification,
) -> tuple[Keyring, KeyChain]:
    """Follow `keys.jsonl` from the trusted root (rules R11, R12). Problems go to `result`; the
    keyring maps every trusted key id to (rank, key): the valid chain, then the extra keys."""
    root_key = public_keys[0]
    root_id = key_id(root_key)
    start = ChainKey(root_id, _hex_public(root_key))
    chain = KeyChain(keys=[start])
    unreadable = False
    head_bytes: bytes | None = None
    if head_commit is not None:
        try:
            head_bytes = _committed_file(repo, head_commit, KEY_FILE)
        except AnchorError as exc:
            unreadable = True
            result.repository_problems.append(
                AnchorProblem("key_change_invalid", f"record 1: unreadable ({exc})")
            )
    seen = [d for _c, v in versions if (d := v.get(KEY_FILE)) is not None]
    if not unreadable:
        if head_bytes is None and seen:
            result.repository_problems.append(
                AnchorProblem("key_file_removed", "the key file was removed from the repository")
            )
        elif head_bytes is not None and any(not head_bytes.startswith(v) for v in seen):
            result.repository_problems.append(
                AnchorProblem("key_file_rewritten", "the key file's history isn't append-only")
            )
    if head_bytes and head_commit is not None:
        try:
            text = head_bytes.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
            result.repository_problems.append(
                AnchorProblem("key_change_invalid", "record 1: unreadable (not UTF-8)")
            )
        chain = walk_key_chain(
            text,
            root=start,
            repo_roots=_root_commits(repo, head_commit),
            verify=_verify_hex,
            derive_id=_derive_id,
        )
        if chain.problem is not None:
            result.repository_problems.append(chain.problem)
    keyring: Keyring = {}
    for rank, link in enumerate(chain.keys):
        key = (
            root_key
            if rank == 0
            else Ed25519PublicKey.from_public_bytes(bytes.fromhex(link.public_key))
        )
        keyring[link.key_id] = (rank, key)
    result.chain = [k.key_id for k in chain.keys]
    for extra in public_keys[1:]:
        extra_id = key_id(extra)
        if extra_id not in keyring:
            keyring[extra_id] = (len(chain.keys) + len(result.extra_keys), extra)
            result.extra_keys.append(extra_id)
    if result.extra_keys:
        result.notes.append(
            "INFO anchors verified by explicitly trusted keys: "
            + ", ".join([*result.chain[-1:], *result.extra_keys])
        )
    return keyring, chain


def _retirements(
    repo: Path,
    head_commit: str | None,
    versions: _Versions,
    result: AnchorVerification,
    chain: KeyChain,
    tail_valid_until: str | None,
) -> Callable[[str], dict[str, Retirement]]:
    """For each stream file, what the retired keys (and a bounded tail) may still have in it
    (rule R13). A merge commit in a history that has key records is a repository problem and
    leaves every retired key with no trusted line."""
    nothing: Callable[[str], dict[str, Retirement]] = lambda _name: {}  # noqa: E731
    if head_commit is None or (not chain.records and tail_valid_until is None):
        return nothing
    not_linear = False
    if chain.records and _git(repo, "rev-list", "--merges", head_commit).strip():
        not_linear = True
        result.repository_problems.append(
            AnchorProblem(
                "anchor_history_not_linear",
                "the anchor branch has a merge commit: retired keys' lines can't be placed",
            )
        )
    position = {commit: i for i, (commit, _v) in enumerate(versions)}
    retiring: list[int] = []
    for n in range(1, len(chain.records) + 1):
        retiring.append(
            next(i for i, (_c, v) in enumerate(versions) if _lines(v.get(KEY_FILE)) >= n)
        )
    bound: int | None = None
    if tail_valid_until is not None:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tail_valid_until):
            raise AnchorError("--tail-valid-until needs a full commit id")
        if tail_valid_until not in position:
            raise AnchorError("--tail-valid-until names a commit that isn't on the anchor branch")
        bound = position[tail_valid_until]

    def for_file(name: str) -> dict[str, Retirement]:
        found: dict[str, Retirement] = {}
        for i, record in enumerate(chain.records):
            lines = 0 if not_linear else _lines(versions[retiring[i]][1].get(name))
            found[chain.keys[i].key_id] = Retirement(lines, record.at, record.compromised_since)
        if bound is not None:
            lines = 0 if not_linear else _lines(versions[bound][1].get(name))
            found[chain.keys[-1].key_id] = Retirement(lines)
        return found

    return for_file


def verify_anchors(
    store: Store,
    repo: Path,
    public_key: Ed25519PublicKey,
    *,
    streams: list[str] | None = None,
    ref: str | None = DEFAULT_REF,
) -> list[StreamAnchorReport]:
    """Check each stream against its committed anchors (module docstring); the stream reports
    of `verify_anchor_repository` with one trusted key. Repository problems are not returned
    here: callers that must show them use `verify_anchor_repository`."""
    return verify_anchor_repository(store, repo, [public_key], streams=streams, ref=ref).streams


def verify_anchor_repository(
    store: Store,
    repo: Path,
    public_keys: Sequence[Ed25519PublicKey],
    *,
    streams: list[str] | None = None,
    ref: str | None = DEFAULT_REF,
    tail_valid_until: str | None = None,
) -> AnchorVerification:
    """Check each stream against its committed anchors and the repository's key chain.

    The first public key is the trusted **root**; the key file `keys.jsonl` (if any) is followed
    from it (rules R10, R11). Further keys are explicitly trusted extra keys (the lost-key
    procedure, R14): they rank after the chain tail and require `tail_valid_until`, the commit
    that bounds the tail's lines. `ref` names the anchor branch to read (default
    `ledger-anchors`; a remote-tracking ref such as `origin/ledger-anchors` works too);
    main/master are refused. Any git failure, a shallow clone or a missing ref is an
    `AnchorError`: "couldn't check" is never reported as "clean".
    """
    _require_crypto()
    if not public_keys:
        raise AnchorError("a public key is needed to verify anchors")
    if len(public_keys) > 1 and tail_valid_until is None:
        raise AnchorError(
            "extra trusted keys need --tail-valid-until <commit>: the commit that ends the "
            "lost key's anchors"
        )
    root = repo
    _git_dir(root)
    if ref is not None:
        head_commit: str | None = _ref_commit(root, ref)
    else:
        head_commit = _head_commit(root)
        current = _branch_or_none(root)
        if head_commit is not None and current in PROTECTED_BRANCHES:
            raise AnchorError(f"HEAD is on '{current}': anchors never live there")
    known: set[str] = set()
    if head_commit is not None:
        listed = _git(root, "ls-tree", "--name-only", head_commit)
        known = {n for n in listed.decode("utf-8", "replace").splitlines() if _FILE.match(n)}
        known |= _ever_files(root, head_commit)
        if streams is not None:  # only the files of the streams asked for are read
            wanted = {anchor_filename(x) for x in streams}
            known &= wanted
        versions = _versions_by_commit(root, head_commit, [*sorted(known), KEY_FILE])
    else:
        versions = []
    history: dict[str, list[bytes]] = {
        n: [d for _c, v in versions if (d := v[n]) is not None] for n in known
    }
    result = AnchorVerification(streams=[])
    keyring, chain = _key_chain(root, head_commit, public_keys, versions, result)
    retirements = _retirements(root, head_commit, versions, result, chain, tail_valid_until)

    def stream_of(filename: str) -> str:
        return filename[: -len(".jsonl")].replace("__", ":", 1)

    if streams is not None:
        names = sorted(set(streams))  # exactly the streams asked for, nothing else is read
    else:
        names = sorted(set(store.streams()) | {stream_of(n) for n in known})
    checked_out = _head_commit(root)  # the working copy is only compared if this is the ref
    for stream in names:
        events, _ = store.snapshot(stream)
        filename = anchor_filename(stream)
        try:
            data = _committed_file(root, head_commit, filename)
        except AnchorError as exc:
            result.streams.append(
                StreamAnchorReport(
                    stream, 0, 0, [AnchorProblem("unreadable_anchor_file", str(exc))]
                )
            )
            continue
        try:
            anchors = parse_anchor_lines((data or b"").decode("utf-8"), stream=stream)
        except (UnicodeDecodeError, AnchorFileError) as exc:
            result.streams.append(
                StreamAnchorReport(
                    stream, 0, 0, [AnchorProblem("unreadable_anchor_file", f"{filename}: {exc}")]
                )
            )
            continue
        report = StreamAnchorReport(stream, len(anchors), 0)
        skip: set[int] = set()
        for index, anchor in enumerate(anchors):
            entry = keyring.get(anchor.key_id)
            if entry is None:
                report.problems.append(
                    AnchorProblem(
                        "wrong_key", f"anchor at seq {anchor.seq} was signed by another key"
                    )
                )
                skip.add(index)
                continue
            try:
                entry[1].verify(bytes.fromhex(anchor.sig), anchor.message())
            except (InvalidSignature, UnsafeNumberError):
                report.problems.append(
                    AnchorProblem(
                        "bad_signature", f"anchor at seq {anchor.seq} isn't validly signed"
                    )
                )
                skip.add(index)
        rule_problems, rejected = check_key_rules(
            anchors,
            {k: v[0] for k, v in keyring.items()},
            retirements(filename),
            skip,
        )
        report.problems += rule_problems
        verified = [a for i, a in enumerate(anchors) if i not in skip and i not in rejected]
        report.unanchored_events = max(0, unanchored_events(verified, events))
        report.problems += check_anchors(stream, verified, events)
        if head_commit is not None and filename in known:
            head_bytes = data or b""
            for version in history.get(filename, []):
                if not head_bytes.startswith(version):
                    code = "history_rewritten" if data is not None else "anchor_file_removed"
                    detail = (
                        "the anchor file's history isn't append-only"
                        if data is not None
                        else "the anchor file was removed from the repository"
                    )
                    report.problems.append(AnchorProblem(code, detail))
                    break
            if checked_out == head_commit and _working_copy_differs(root, head_commit, filename):
                report.problems.append(
                    AnchorProblem(
                        "uncommitted_anchor_file", f"{filename} differs from what is committed"
                    )
                )
        result.streams.append(report)
    return result


def _check_ssh_key(key: Path, repo: Path) -> Path:
    """The deploy key used to push: a private regular file, outside the anchor repository."""
    try:
        resolved = key.resolve(strict=True)
    except OSError:
        raise AnchorError(f"the SSH key {key} wasn't found") from None
    if not resolved.is_file():
        raise AnchorError(f"the SSH key {key} isn't a file")
    if os.name == "posix" and resolved.stat().st_mode & 0o077:
        raise AnchorError(f"{key} is readable by others; run: chmod 600 {key}")
    root = repo.resolve()
    if root == resolved.parent or root in resolved.parents:
        raise AnchorError("the SSH key must not live inside the anchor repository")
    return resolved


def _remote_url(repo: Path, remote: str) -> str:
    """The one SSH address the remote pushes to; refuses any config that could send it elsewhere
    (several `url`s, a `pushurl`, or `insteadOf`/`pushInsteadOf` rewrites)."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", remote):
        raise AnchorError(f"'{remote}' isn't a usable remote name")
    urls = (
        _git(repo, "config", "--get-all", f"remote.{remote}.url", check=False).decode().splitlines()
    )
    if not urls:
        raise AnchorError(f"the anchor repository has no remote '{remote}'")
    rewrites = _git(repo, "config", "--get-regexp", r"^url\..*insteadof$", check=False)
    pushurl = _git(repo, "config", "--get-all", f"remote.{remote}.pushurl", check=False)
    if len(urls) != 1 or pushurl.strip() or rewrites.strip():
        raise AnchorError(
            f"remote '{remote}' must have exactly one url and no pushurl or insteadOf rewrite"
        )
    if not _SSH_URL.match(urls[0]):
        raise AnchorError(f"remote '{remote}' must be an SSH address (git@host:owner/repo.git)")
    return urls[0]


def _is_key_append(repo: Path, commit: str) -> bool:
    """The commit changes `keys.jsonl` as a pure append: no deleted line, and the parent's file
    is a prefix of the new one."""
    parents = _git(repo, "rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
    if len(parents) != 1:
        return False
    stat_line = _git(
        repo, "diff", "--numstat", "--no-renames", parents[0], commit, "--", KEY_FILE
    ).decode()
    fields = stat_line.split()
    if len(fields) < 3 or fields[1] != "0":
        return False
    before = _committed_file(repo, parents[0], KEY_FILE) or b""
    after = _committed_file(repo, commit, KEY_FILE) or b""
    return after.startswith(before)


def _check_unpushed(repo: Path, remote: str) -> None:
    """Commits not on `remote` may only be the exporter's own commits (a failed earlier push):
    each one made by the exporter's identity and changing either only anchor files, or only
    `keys.jsonl` as a pure append (a key rotation). Anything else on the anchor branch (an empty
    commit, a root commit, another author) would be published by the next push."""
    if _head_commit(repo) is None:
        return
    out = _git(
        repo,
        "log",
        "--no-show-signature",
        "--root",
        "-m",
        "--full-history",
        "--no-renames",
        "--format=COMMIT %H %ce",
        "--name-only",
        "HEAD",
        "--not",
        f"--remotes={remote}",
    ).decode("utf-8", "replace")
    commits: list[tuple[str, str, list[str]]] = []
    for line in out.splitlines():
        if line.startswith("COMMIT "):
            _, sha, email = line.split(" ", 2)
            commits.append((sha, email, []))
        elif line and commits:
            commits[-1][2].append(line)
    for sha, email, files in commits:
        anchors_only = bool(files) and all(_FILE.match(name) for name in files)
        key_only = files == [KEY_FILE] and _is_key_append(repo, sha)
        if email != ANCHOR_EMAIL or not (anchors_only or key_only):
            raise AnchorError(
                "the anchor branch has commits that aren't on the remote and aren't the "
                "exporter's anchors; pushing would publish them. Push or drop them first"
            )


def plan_anchor_branch(repo: Path, branch: str = DEFAULT_REF, remote: str = "origin") -> bool:
    """Check the anchor branch can be made current; True if the first export must create it.

    The branch is `branch` itself, or a new branch cut from wherever the checkout is (a fresh
    clone sits on `main`, which anchors never touch). A branch that exists but isn't checked out
    is refused: switching branches would change the working tree under an operator's hands. The
    branch may carry unpushed commits only if they are anchor commits.
    """
    if branch in PROTECTED_BRANCHES or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}", branch
    ):
        raise AnchorError(f"'{branch}' can't be the anchor branch")
    _git_dir(repo)
    _refuse_shallow(repo)
    current = _branch_or_none(repo)
    if current == branch:
        _check_unpushed(repo, remote)
        return False
    exists = _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).strip()
    if exists:
        raise AnchorError(f"branch '{branch}' exists but isn't checked out: git switch {branch}")
    tracked = _git(
        repo, "for-each-ref", "--format=%(refname)", f"refs/remotes/*/{branch}", check=False
    ).strip()
    if tracked:
        raise AnchorError(
            f"the remote already has '{branch}' (a new local branch would never push): "
            f"git switch {branch}"
        )
    start = _head_commit(repo)
    if start is not None:
        on_remote = _git(
            repo,
            "for-each-ref",
            "--contains",
            start,
            "--format=%(refname)",
            f"refs/remotes/{remote}",
        ).strip()
        if not on_remote:
            raise AnchorError(
                f"the checkout has commits that aren't on '{remote}'; the new branch would "
                "publish them. Push or drop them first"
            )
    return True


def ensure_anchor_branch(
    repo: Path,
    branch: str = DEFAULT_REF,
    *,
    remote: str = "origin",
    ssh_key: Path | None = None,
) -> bool:
    """Make `branch` the checked-out branch, creating it at the current commit on first use.

    Returns True if it was created. Nothing in the working tree changes: the new branch starts
    at the commit the checkout is on (or empty, in a repository with no commits yet). With
    `ssh_key`, the remote is asked first (a clone made before the first push, or a single-branch
    clone, doesn't know the remote already has the branch; a new local branch would never push).
    """
    if not plan_anchor_branch(repo, branch, remote):
        return False
    if ssh_key is not None:
        key = _check_ssh_key(ssh_key, repo)
        _remote_url(repo, remote)
        listed = _git(
            repo,
            "ls-remote",
            "--heads",
            remote,
            f"refs/heads/{branch}",
            ssh_key=key,
            timeout=PUSH_TIMEOUT,
        ).strip()
        if listed:
            raise AnchorError(
                f"the remote already has '{branch}': git fetch {remote} {branch} and "
                f"git switch {branch}"
            )
    start = _head_commit(repo)
    ref = f"refs/heads/{branch}"
    if start is not None:
        _git(repo, "update-ref", ref, start, "")  # empty old value: the ref must not exist yet
    _git(repo, "symbolic-ref", "HEAD", ref)
    return True


def preflight_push(repo: Path, ssh_key: Path, remote: str = "origin") -> None:
    """Checks a push needs, without pushing: the branch can be made current, the key is private
    and outside the repository, and the remote is an SSH address. Raises `AnchorError`."""
    plan_anchor_branch(repo, DEFAULT_REF, remote)
    _check_ssh_key(ssh_key, repo)
    _remote_url(repo, remote)


@dataclass
class PushReport:
    """What a push did."""

    remote: str
    branch: str
    created_on_remote: bool = False
    up_to_date: bool = False


def push_anchors(
    repo: Path,
    ssh_key: Path,
    *,
    remote: str = "origin",
    branch: str = DEFAULT_REF,
) -> PushReport:
    """Push the anchor branch (and nothing else) to its remote with the deploy key.

    The refspec is fixed to `refs/heads/<branch>:refs/heads/<branch>`: never `main`, never a
    force, so a remote that has moved on is a refusal (anchor history only ever appends). The
    remote must be an SSH address; the key is used alone (`IdentitiesOnly`, no ssh config, host
    key checked, no prompts). The branch is created on the remote by the first push.
    """
    if branch in PROTECTED_BRANCHES:
        raise AnchorError(f"'{branch}' is a main branch: anchors never go there")
    _git_dir(repo)
    if _branch_or_none(repo) != branch:
        raise AnchorError(f"the anchor repository isn't on branch '{branch}'")
    key = _check_ssh_key(ssh_key, repo)
    url = _remote_url(repo, remote)
    resolved = _git(repo, "remote", "get-url", "--push", "--all", remote).decode().split()
    if resolved != [url]:
        raise AnchorError(f"remote '{remote}' doesn't resolve to its checked address")
    refspec = f"refs/heads/{branch}:refs/heads/{branch}"
    try:
        done = _git(
            repo,
            "push",
            "--porcelain",
            "--no-verify",
            "--no-follow-tags",
            "--no-recurse-submodules",
            "--",
            remote,  # a name git resolved to exactly the checked address (above)
            refspec,
            ssh_key=key,
            timeout=PUSH_TIMEOUT,
        ).decode("utf-8", "replace")
    except AnchorError:
        raise AnchorError(
            f"pushing '{branch}' to '{remote}' failed: check the deploy key and the host key, and "
            "whether the remote branch moved on (fetch it and check it out; anchors are never "
            "force-pushed)"
        ) from None
    flags = [line[:1] for line in done.splitlines() if "refs/heads/" in line]
    if not flags or "!" in flags:
        raise AnchorError(f"the push of '{branch}' to '{remote}' was rejected")
    return PushReport(
        remote=remote,
        branch=branch,
        created_on_remote="*" in flags,
        up_to_date="=" in flags,
    )


# -- rotation (R14) ------------------------------------------------------------------------------


@dataclass
class RotateReport:
    """What a rotation did, step by step (each step is skipped when already done)."""

    prev_key_id: str
    new_key_id: str
    chained: bool
    n: int | None = None
    commit: str | None = None
    record_written: bool = False
    pushed: bool | None = None  # None: no push was asked for
    event_written: bool = False


@contextlib.contextmanager
def _rotate_lock(repo: Path) -> Iterator[None]:
    """One rotate at a time on a checkout: an exclusive, non-blocking lock in `.git`."""
    path = _git_dir(repo) / "nuramesh-rotate.lock"
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise AnchorError("the rotate lock can't be opened") from None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AnchorError("another rotate is running on this checkout") from None
        yield
    finally:
        os.close(descriptor)


def _require_clean_checkout(repo: Path) -> str:
    """The anchors branch must be checked out, not main/master, with a commit and no edits to
    tracked files. Returns the tip commit."""
    _git_dir(repo)
    if not (repo / ".git").is_dir():
        raise AnchorError("rotating a key needs a checkout of the anchor branch, not a bare repo")
    if _branch(repo) in PROTECTED_BRANCHES:
        raise AnchorError("anchors never go on main/master: check out the anchor branch")
    tip = _head_commit(repo)
    if tip is None:
        raise AnchorError("the anchor branch has no commit yet: export anchors first")
    dirty = _git(
        repo,
        "status",
        "--porcelain",
        "--untracked-files=no",
        env_extra={"GIT_WORK_TREE": str(repo)},
    ).strip()
    if dirty or _working_copy_differs(repo, tip, KEY_FILE):
        raise AnchorError("the anchor checkout has uncommitted changes; commit or restore them")
    return tip


def _repo_root_commit(repo: Path, tip: str) -> str:
    roots = sorted(_root_commits(repo, tip))
    if len(roots) != 1:
        raise AnchorError("the anchor branch must have exactly one root commit")
    return roots[0]


def _unrooted_chain(repo: Path, tip: str) -> KeyChain:
    """The key file's valid prefix, walked without knowing the root key (rotate isn't given it;
    verifiers always check the first record against their trusted root)."""
    data = _committed_file(repo, tip, KEY_FILE) or b""
    chain = walk_key_chain(
        data.decode("utf-8", "replace"),
        root=None,
        repo_roots=_root_commits(repo, tip),
        verify=_verify_hex,
        derive_id=_derive_id,
    )
    if chain.problem is not None:
        raise AnchorError(f"the key file isn't valid ({chain.problem.detail}): fix it first")
    return chain


def _root_key_id(repo: Path, tip: str) -> str | None:
    """The key id of every anchor line in the stream files, if they all agree (None: no anchors);
    more than one id is an error."""
    ids: set[str] = set()
    names = [
        n for n in _git(repo, "ls-tree", "--name-only", tip).decode().splitlines() if _FILE.match(n)
    ]
    for name in names:
        data = _committed_file(repo, tip, name) or b""
        stream = name[: -len(".jsonl")].replace("__", ":", 1)
        try:
            ids |= {a.key_id for a in parse_anchor_lines(data.decode("utf-8"), stream=stream)}
        except (UnicodeDecodeError, AnchorFileError) as exc:
            raise AnchorError(f"{name} can't be read: {exc}") from None
    if len(ids) > 1:
        raise AnchorError("the anchors name more than one key and there is no key file")
    return next(iter(ids)) if ids else None


def _latest_anchor_at(repo: Path, tip: str, signer: str) -> str | None:
    """The latest `at` among the anchor lines `signer` signed in the stream files at `tip`."""
    latest: str | None = None
    names = [
        n for n in _git(repo, "ls-tree", "--name-only", tip).decode().splitlines() if _FILE.match(n)
    ]
    for name in names:
        data = _committed_file(repo, tip, name) or b""
        stream = name[: -len(".jsonl")].replace("__", ":", 1)
        try:
            found = parse_anchor_lines(data.decode("utf-8"), stream=stream)
        except (UnicodeDecodeError, AnchorFileError) as exc:
            raise AnchorError(f"{name} can't be read: {exc}") from None
        for a in found:
            if a.key_id == signer and (latest is None or a.at > latest):
                latest = a.at
    return latest


def _chain_tail(repo: Path, tip: str, chain: KeyChain, *, fallback: str | None) -> str:
    if chain.keys:
        return chain.keys[-1].key_id
    root = _root_key_id(repo, tip)
    if root is not None:
        return root
    if fallback is None:
        raise AnchorError("there are no anchors yet, so the root key can't be told")
    return fallback


def _org_events(store: Store, org_stream: str) -> list[LedgerEvent]:
    out: list[LedgerEvent] = []
    cursor = 0
    while True:
        batch = list(store.read_stream(org_stream, after_seq=cursor, limit=500))
        out.extend(batch)
        if len(batch) < 500:
            return out
        cursor = batch[-1].seq


def _rotation_events(store: Store, org_stream: str) -> list[Mapping[str, object]]:
    return [
        e.payload for e in _org_events(store, org_stream) if e.type == EventType.ANCHOR_KEY_ROTATED
    ]


def _append_rotation_event(store: Store, org_stream: str, payload: dict[str, object]) -> None:
    with store.transaction() as uow:
        uow.append(
            EventDraft(
                stream=org_stream,
                type=EventType.ANCHOR_KEY_ROTATED,
                actor="system:anchor-cli",
                payload=payload,
            )
        )


def _on_remote(repo: Path, commit: str, remote: str) -> bool:
    found = _git(
        repo, "for-each-ref", "--contains", commit, "--format=%(refname)", f"refs/remotes/{remote}"
    )
    return bool(found.strip())


class RotationIncomplete(AnchorError):
    """A rotation stopped after its commit (push or ledger step missing); a rerun can finish it
    (class UNAVAILABLE, F5)."""


def rotate_key(
    store: Store,
    repo: Path,
    new_key: Ed25519PrivateKey,
    *,
    org_stream: str,
    outgoing: Ed25519PrivateKey | None = None,
    lost_old_key: str | None = None,
    compromised_since: str | None = None,
    push_key: Path | None = None,
    remote: str = "origin",
    now: Callable[[], datetime] | None = None,
) -> RotateReport:
    """Rotate the signing key (rule R14): a signed key-change record in `keys.jsonl`, committed
    on its own, pushed (with `push_key`) and then recorded as `anchor.key_rotated` in the
    organization stream. Every step is skipped when already done, so a rerun finishes the missing
    ones and never writes a second record. With `lost_old_key` (a key id; the old private key is
    gone) no record is written: only the ledger event, the verifier is given both public keys.
    Raises `AnchorError`; keys are never printed.
    """
    _require_crypto()
    if (outgoing is None) == (lost_old_key is None):
        raise AnchorError("give exactly one of the old key and the lost old key's id")
    since: str | None = None
    if compromised_since is not None:
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", compromised_since):
            raise AnchorError("--compromised-since isn't a valid time (canonical form needed)")
        try:
            datetime.strptime(compromised_since, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            raise AnchorError("--compromised-since isn't a valid time") from None
        since = compromised_since
        if since > anchor_time((now or store.now)()):
            raise AnchorError("--compromised-since can't be in the future")
    root = repo
    with _rotate_lock(root):
        tip = _require_clean_checkout(root)
        if push_key is not None:
            preflight_push(root, push_key, remote)
        chain = _unrooted_chain(root, tip)
        new_id = key_id(new_key.public_key())
        if outgoing is None:
            assert lost_old_key is not None  # noqa: S101 (narrowing for the type checker)
            return _rotate_lost(store, root, tip, chain, lost_old_key, new_id, org_stream, since)
        old_id = key_id(outgoing.public_key())
        at = anchor_time((now or store.now)())
        if since is not None and since > at:
            raise AnchorError("--compromised-since can't be in the future")
        report = RotateReport(old_id, new_id, chained=True)
        last = chain.records[-1] if chain.records else None
        if last is not None and (last.prev_key_id, last.new_key_id) == (old_id, new_id):
            if since is not None and since != last.compromised_since:
                raise AnchorError(
                    "--compromised-since differs from the signed key record; the claim of a "
                    "written record can't change (leave the flag out to finish the rotation)"
                )
            since = last.compromised_since  # the event states what the verifier enforces
            report.n = last.n
            record_line = last.line()
            report.commit = _record_commit(root, tip, last.n)
        else:
            tail = _chain_tail(root, tip, chain, fallback=old_id)
            if old_id != tail:
                raise AnchorError(
                    "the old key isn't the chain's current key; if it is lost, use --lost-old-key"
                )
            if new_id == old_id or new_id in {k.key_id for k in chain.keys}:
                raise AnchorError("the new key has been used before; make a new one (keygen)")
            if last is not None and at < last.at:
                raise AnchorError(
                    f"the clock says {at}, earlier than the previous key record ({last.at}); "
                    "a record dated before it would fail verification (R11): fix the clock"
                )
            latest = _latest_anchor_at(root, tip, old_id)
            if latest is not None and latest >= at:
                raise AnchorError(
                    f"an anchor signed by the outgoing key is dated {latest}, not before the "
                    f"record's time {at}; it would fail verification (R13 b): fix the clock"
                )
            n = len(chain.records) + 1
            fields = {
                "n": n,
                "repo_root": _repo_root_commit(root, tip),
                "prev_key_id": old_id,
                "new_key_id": new_id,
                "new_public_key": _hex_public(new_key.public_key()),
                "at": at,
                "compromised_since": since,
            }
            message = key_change_message(**fields)  # type: ignore[arg-type]
            record = KeyChange(
                **fields,  # type: ignore[arg-type]
                sig_old=outgoing.sign(message).hex(),
                sig_new=new_key.sign(message).hex(),
            )
            record_line = record.line()
            existing = _committed_file(root, tip, KEY_FILE) or b""
            report.commit = _commit_files(
                root,
                tip,
                {KEY_FILE: existing + (record_line + "\n").encode("utf-8")},
                f"anchors: key change {n} ({old_id} to {new_id})",
            )
            report.n, report.record_written = n, True
        if push_key is not None:
            assert report.commit is not None  # noqa: S101
            if _on_remote(root, report.commit, remote):
                report.pushed = True
            else:
                report.pushed = False
                try:
                    push_anchors(root, push_key, remote=remote)
                except AnchorError as exc:
                    raise RotationIncomplete(
                        f"key change {report.n} is committed locally, pushed: no ({exc}); the "
                        "remote may have moved. Nothing was recorded in the ledger"
                    ) from None
                report.pushed = True
        digest = hashlib.sha256(record_line.encode("utf-8")).hexdigest()
        done = [p for p in _rotation_events(store, org_stream) if p.get("record_sha256") == digest]
        if not done:
            payload = key_rotated_payload(
                prev_key_id=old_id,
                new_key_id=new_id,
                chained=True,
                n=report.n,
                record_sha256=digest,
                compromised_since=since,
                commit=report.commit or tip,
            )
            try:
                _append_rotation_event(store, org_stream, payload)
            except Exception:  # noqa: BLE001 (any store failure: name the missing step, no text)
                raise RotationIncomplete(
                    f"key change {report.n} is committed, ledger event missing: run the same "
                    "command again to record it"
                ) from None
            report.event_written = True
        return report


def _record_commit(repo: Path, tip: str, n: int) -> str:
    """The first commit on the branch whose key file holds `n` records."""
    for commit, found in _versions_by_commit(repo, tip, [KEY_FILE]):
        if _lines(found.get(KEY_FILE)) >= n:
            return commit
    raise AnchorError("the key record's commit wasn't found")


def _rotate_lost(
    store: Store,
    repo: Path,
    tip: str,
    chain: KeyChain,
    lost: str,
    new_id: str,
    org_stream: str,
    since: str | None,
) -> RotateReport:
    """The lost-old-key procedure: validate, then write only the ledger event."""
    if not re.fullmatch(r"[0-9a-f]{16}", lost):
        raise AnchorError("--lost-old-key needs a key id (16 hex characters)")
    report = RotateReport(lost, new_id, chained=False, commit=tip)
    events = _rotation_events(store, org_stream)
    # a rerun of the same lost-key pair is a second loss, not an idempotent no-op (C5 (2), KR-41)
    tail = _chain_tail(repo, tip, chain, fallback=None)
    if any(p.get("chained") is False and p.get("prev_key_id") == tail for p in events):
        raise AnchorError(
            "the chain's current key is already a lost key; a second key loss can't be handled "
            "here: start again from a fresh genesis (a new anchors chain with a new root key)"
        )
    if lost != tail:
        raise AnchorError(
            "--lost-old-key must be the chain's current key (the last one rotated to)"
        )
    if new_id == lost or new_id in {k.key_id for k in chain.keys}:
        raise AnchorError("the new key has been used before; make a new one (keygen)")
    payload = key_rotated_payload(
        prev_key_id=lost,
        new_key_id=new_id,
        chained=False,
        n=None,
        record_sha256=None,
        compromised_since=since,
        commit=tip,
    )
    try:
        _append_rotation_event(store, org_stream, payload)
    except Exception:  # noqa: BLE001
        raise AnchorError("the ledger event couldn't be written; nothing was recorded") from None
    report.event_written = True
    return report
