# nuramesh-verify: the independent ledger verifier (E7)

One file, `nuramesh_verify.py`, Python 3.12+, standard library only. It imports nothing from NuraMesh and shares no code
with it; it checks a ledger export against signed anchors, offline.

```bash
nuramesh ledger export --team my-team --out ledger.jsonl       # on the operator's side
python3 nuramesh_verify.py ledger.jsonl --anchors ./anchors-checkout --pubkey anchor.pub
```

Exit code 0: no problem found; 1: problems found; 2: the check could not run.

Optional `--git` also checks that the anchors repository's history is append-only. It needs the `git` program (asked
for and missing is exit 2, never a quiet skip); the core never needs it. The output names the mode that ran
(`chain`, `chain+anchors` or `chain+anchors+git`) in text and JSON. With `--git`, `--git-ref BRANCH` reads
another branch and `--git-commit <40 lower-case hex digits>` reads exactly that commit (not both). ES256 anchors are not read yet.

- What it checks, what it does not, how to read the output: an internal document
- The specification it implements: the internal specification
- Worked cases (every problem code): the internal specification
- Tests: `tests/verifier/`

The file is licensed Apache 2.0 like the rest of the open core. It can be copied out on its own.
