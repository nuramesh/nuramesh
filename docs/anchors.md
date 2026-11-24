# Ledger anchors: signing, committing and pushing (R1.04a)

Two different keys, two different jobs:

| Key | What it does | Where it lives |
| --- | --- | --- |
| **Signing key** (Ed25519, `nuramesh anchor keygen`) | Signs each anchor (stream, sequence, head hash, time). Verifiers hold the **public** half. | A file outside Git and the database, mode 0600 (`--key`). |
| **Deploy key** (ed25519 SSH key) | Lets the exporter **push** to `nuramesh/nuramesh-anchors`. It signs nothing. | A file outside every repository; its path comes from `NURAMESH_ANCHOR_SSH_KEY`. Never a flag, never printed. |

## Export and push

```bash
# the anchor repository is a clone of nuramesh/nuramesh-anchors (it starts on main)
export NURAMESH_ANCHOR_SSH_KEY=~/.ssh/nuramesh_anchors_ed25519
nuramesh anchor export --team <team> --repo <clone> --key <signing-key> --push
nuramesh try <team> ... --anchor-repo <clone> --anchor-key <signing-key> --anchor-push   # at run end
```

- `--push` makes `ledger-anchors` the checked-out branch on first use (cut from the commit the clone is
  on, so the README comes along), commits the new anchors, then pushes **exactly**
  `refs/heads/ledger-anchors:refs/heads/ledger-anchors`. The first push creates the branch on the remote.
- It never pushes `main`, never forces, and never sends tags. If the remote branch has moved on, the push is
  refused and the anchors stay committed locally (fetch, check the remote branch out, export again).
- The remote must have exactly one SSH `url` (no `pushurl`, no `insteadOf` rewrite); the push goes to the remote by name, after git is asked what that name resolves to and it matches the checked address exactly.
- A second machine: clone, then `git fetch origin ledger-anchors && git switch ledger-anchors` first. Export asks the remote and refuses to cut a new branch when it already has `ledger-anchors` (single-branch clones included), or when the clone has commits that aren't on the remote. A checked-out `ledger-anchors` may carry unpushed commits only if the exporter made them and they change only anchor files (no empty, root or foreign commits).
- The remote must be an SSH address. ssh runs with that key only (`IdentitiesOnly`), no ssh config file,
  the host key must already be known (`ssh -T git@github.com` once), and no prompts (`BatchMode`).
- Everything is checked before the run spends anything: key mode (0600) and location (not in the repo), the
  remote, and the branch.
- Hourly: run `anchor export ... --push` from cron or launchd; a run that has nothing new commits nothing and
  pushes nothing new.
- **Organization streams:** `run --anchor-repo` anchors the run's stream and its own organization's stream
  (budgets, kill switch, sign-ins); a run refused at start or aborted by a second interrupt is not anchored
  (the hourly export covers it); `anchor export` without `--stream` anchors every stream. The ledger
  page reads "Verified" only when every stream has a signed anchor. Entries written after the last anchor
  (a sign-in) are counted as newer than the anchor, not as a problem; the hourly job anchors them. Spec:
  The internal specification.

## Remote verification (R1.30 PR 5, H-13)

`nuramesh anchor verify TEAM --repo <checkout> --pubkey <file> --remote <name|git@host:owner/repo.git>`
reads the anchors from the remote through **a clone the verifier owns** (a bare repository in a 0700
directory, `--clone-dir`, default `~/.cache/nuramesh/anchor-clones/<hash of the address>`). It never uses
your working checkout.

- The deploy key comes from `NURAMESH_ANCHOR_SSH_KEY` (never a flag). `--remote` takes a remote name of
  `--repo` or an SSH address, nothing else: no http, no `file://`, no path.
- Each run asks the remote for the branch tip, fetches it into a scratch ref, and accepts it **only if the last
  verified tip (the pin, `refs/nuramesh/verified`) is an ancestor of it**. The pin moves only after a run with
  no problem. The verifier never resets, forces or pulls.
- A force-push, a replaced or shortened history, or a deleted branch is **tampering**: the report prints
  `REPOSITORY anchor_history_rewritten: ...` and nothing else, the exit code is 1, and the clone and pin stay
  as they were, so the next run says the same. An unreachable remote is
  "could not check" (exit 5), never "clean". A missing branch on first use is "no anchors found on the remote"
  (an error, exit 2, never "unanchored"); a bad option is exit 2. A fetch has no total size cap beyond the
  per-file read cap, so point it only at a remote you control.
- `--pubkey` may be repeated and `--tail-valid-until <commit>` given, as for `ledger verify --anchors` (see
  Key rotation below); the key chain is followed from the first key in the clone the verifier owns.
- `--output json` prints `{ok, tip, repository: {ok, problems, chain, extra_keys}, streams: [...]}`.
- `--expect-tip <commit id>` takes a commit id from an out-of-band copy and fails with
  `anchor_history_rewritten` if it is not in the fetched history: use it for the first run of a fresh clone.
- Limits: a clone made for the first time from an already rewritten remote cannot know it (trust on first
  use; use `--expect-tip`); deleting the clone directory resets the pin; the server-side half is a GitHub
  ruleset (no force-push, no deletion on `ledger-anchors`, a design decision).

## Checking without NuraMesh

An auditor can check a ledger export against these anchors offline with a separate program that uses only the Python standard library and shares no code with NuraMesh: `verifier/nuramesh_verify.py`, described in the auditor guide (E7). By default it does not check this repository's commit history for rewrites; its optional `--git` mode does (needs `git`), and so does `nuramesh ledger verify --anchors`.

## Key rotation (R1.30 PR 5, H-14)

The signing key can be replaced without losing the trust in older anchors. A rotation is a **signed key-change
record**: one line of `keys.jsonl` at the root of the anchors branch, signed by both the old and the new key. The
verifier starts from the public key you give it (the **root**, the first `--pubkey`) and follows the records.

```bash
nuramesh anchor keygen --private new.key --public new.pub
nuramesh anchor rotate TEAM --repo ./anchors --key old.key --new-key new.key [--compromised-since 2026-11-05T12:00:00Z] [--push]
nuramesh anchor export --team TEAM --repo ./anchors --key new.key --push    # fresh heads under the new key
nuramesh ledger verify --team TEAM --anchors ./anchors --pubkey old.pub      # root = the FIRST key ever used
```

- `rotate` needs the anchors branch checked out and clean. It commits one commit that touches only `keys.jsonl`,
  pushes it with `--push` (the deploy key from `NURAMESH_ANCHOR_SSH_KEY`, as `export --push`), and **then** records
  the ledger event `anchor.key_rotated` on the organization stream (key ids, record hash and commit only; no key,
  path or host). A rerun finishes only the missing steps and never writes a second record. A retired key can no
  longer `export`.
- Anchors signed before and after a rotation both verify. An anchor signed by a retired key counts only if its line
  was already in the file when the key was retired (git order, not the signer's clock), is dated before the
  retirement, and, with `--compromised-since`, before the claimed compromise. Within one file the keys never go
  back to an older one. A forged, unsigned or reordered record is reported as a **repository problem**
  (`REPOSITORY key_change_invalid: ...`, `key_file_rewritten`, `key_file_removed`), prints before the stream
  lines, and makes the exit code and `/v1/ledger/verify` (`verified: false`) fail even when every stream is clean.
  A merge commit in the history of a repository that has key records is `anchor_history_not_linear`.
- **Lost old key.** `anchor rotate TEAM --repo R --new-key new.key --lost-old-key <old key id>` writes **no** key
  record (nobody can sign for the lost key); it only records the ledger event (with `chained: false`) and prints the
  commit that ends the lost key's anchors. Verify with both public keys and that commit:
  `--pubkey old.pub --pubkey new.pub --tail-valid-until <commit>` (also on `serve --anchors`). The flag is **required**
  whenever a second `--pubkey` is given. A second key loss after this is not handled: start again from a fresh
  genesis (a new anchors chain with a new root key).
- Limits: the chain proves the key holder signed the rotation, not when a compromise happened (`--compromised-since`
  is the operator's claim); someone holding the old private key could sign a forged rotation before the real one is
  committed (the protection is the root you hold and the append-only Git history); a record is bound to its
  repository by the root commit (a fork shares records); the rotating person is not recorded. Run
  `nuramesh anchor verify --remote` right after `rotate --push`: only it moves the pin past the real rotation
  commit (`ledger verify --anchors` pins nothing), and protection against a squashed rotation starts at the first
  pinned verification. The standalone `verifier/nuramesh_verify.py` does
  not follow key changes yet.

## Limits

GitHub deploy keys are per repository, not per branch. The "only `ledger-anchors`" rule is enforced by this
code, and can be enforced on GitHub with a ruleset once the Team plan is visible. Revoke the key with
`gh repo deploy-key delete <id> --repo nuramesh/nuramesh-anchors`.

## Notes (review round 1)

- A rotation that stopped after its commit (the push failed, or the ledger event is missing) exits 5 and names the
  missing step; running the same command again does only that step and writes no second record.
- `--compromised-since` must be the canonical time (`2026-11-05T12:00:00.000000Z`) at the library level and may not
  be in the future (chained or lost-key rotation); the command line accepts `...Z` forms and normalises them.
- The merge check for the key file runs only when the history has at least one key record. On a history with a
  merge and no key file the order of `--tail-valid-until` follows `git rev-list --reverse` (date order, not
  topological), so a merge in an anchors branch is unsupported without a key file.
- `serve --anchors` checks the format of `--tail-valid-until` at start; whether the commit is on the branch is
  checked per request.
