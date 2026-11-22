# Quickstart: your first verified team in 10 minutes (every template, the ledger check and the model catalog)

This extends [quickstart.md](quickstart.md) (install the wheel first, steps 1 to 3 there). It uses
only what is on `main`: the nine shipped templates, `validate`, `try --fake`, `report`,
`ledger verify`, `ledger export` with the standalone verifier, and the model commands. Everything
runs on the **fake model**: no keys, no cost, no network.

Every command below is run by `tests/test_docs_quickstart.py` in a fresh folder, and each sample
output must be what the command really prints (hashes, run ids and counts are masked there), so
this page cannot drift from the product. Run the commands in an empty folder with `nuramesh` on
your `PATH`.

## 1. Pick a template and create the team

`nuramesh init <dir> --template <t>` writes a complete team (`nuramesh.yaml`, `team.py`, and for
`dev-team` a small `repo/`). The templates:

| Template | What the team does | Verifier gates |
| --- | --- | --- |
| `dev-team` | fixes a failing test, runs the suite, asks a human before the staging deploy | one independent reviewer |
| `kanban` | moves cards through Doing and Testing | a reviewer on each build |
| `review-board` | summarizes a change, three reviewers (correctness, security, maintainability) decide | three independent reviewers |
| `scrum` | plans a sprint, two developers build stories, a reviewer and a tester check | a reviewer per story |
| `code-review` | summarizes a change, a code reviewer and a security reviewer decide; nobody reviews their own change | two independent reviewers |
| `devops` | an ops lead has a change planner and a staging checker work through changes | a change reviewer checks the planner |
| `qa` | a QA lead has a test designer and a test runner work through the test plan | a test reviewer checks the designer |
| `sales` | a sales lead has a qualifier and an outreach writer work through leads | a claims checker checks the qualifier |
| `support` | a support lead has a triager and a responder work through tickets | a reply checker checks the triager |

```sh quickstart-templates
nuramesh init dev-team --template dev-team
```

```text
created dev-team from the 'dev-team' template. Next:
  nuramesh validate dev-team/nuramesh.yaml
  nuramesh try dev-team --fake --approve-as human:you --run-id first-run
    (no Podman/Docker/Colima: add --allow-unsandboxed, see the sandbox guide)
  nuramesh serve dev-team
```

## 2. Validate

```sh quickstart-templates
nuramesh validate dev-team/nuramesh.yaml
```

```text
dev-team/nuramesh.yaml: valid
```

## 3. Run on the fake model

```sh quickstart-templates
nuramesh try dev-team --fake --approve-as human:you --allow-unsandboxed --run-id first-run
```

```text
Run first-run
Tasks: 3 done
  [done] first-run-fix-add (dana, attempts 1): Fix the failing add() test in calc.py
      gate has-fix (rule): passed by gate:rule
      gate tests (test_command): passed by gate:test_command
      gate review (verifier): passed by agent:rhea
  [done] first-run-test-suite (theo, attempts 1): Run the whole test suite on the fix
      gate green (rule): passed by gate:rule
  [done] first-run-deploy (omar, attempts 1): Deploy the fix to staging
      gate approve-deploy (human): passed by human:you
Budgets:
  dana-monthly (agent dana): spent 0.001335 of 5.00, reserved 0.000000
  rb-486e135495bd (run first-run): spent 0.003471 of 2.00, reserved 0.000000
  rhea-monthly (agent rhea): spent 0.002136 of 5.00, reserved 0.000000
Spend: $0.003471 (2 calls)
Ledger: run:first-run seq 46, head 1cc85316aa5130c2… (verified)
```

`--approve-as` is a scripted approval so the run finishes alone (the ledger records that);
`--allow-unsandboxed` is local only, see [quickstart.md](quickstart.md) step 4. Nothing is done
until its gates have recorded results, and rhea, the reviewer, never sees dana's reasoning.

The other templates run the same way. Here each is created, validated and run, and only the
summary lines are shown:

```sh quickstart-templates
for t in kanban review-board scrum code-review devops qa sales support; do
  nuramesh init "$t" --template "$t" > /dev/null
  nuramesh validate "$t/nuramesh.yaml"
  nuramesh try "$t" --fake --approve-as human:you --allow-unsandboxed --run-id r1 | grep -E '^(Tasks|Ledger):'
done
```

```text
kanban/nuramesh.yaml: valid
Tasks: 4 done
Ledger: run:r1 seq 72, head 9a79eff914f564e6… (verified)
review-board/nuramesh.yaml: valid
Tasks: 2 done
Ledger: run:r1 seq 51, head 45fb7f7d82901ec7… (verified)
scrum/nuramesh.yaml: valid
Tasks: 5 done
Ledger: run:r1 seq 86, head b566b1a0d05de4ad… (verified)
code-review/nuramesh.yaml: valid
Tasks: 2 done
Ledger: run:r1 seq 44, head 245f8c41bdf33b54… (verified)
devops/nuramesh.yaml: valid
Tasks: 4 done
Ledger: run:r1 seq 72, head 79e17c22df558ba0… (verified)
qa/nuramesh.yaml: valid
Tasks: 4 done
Ledger: run:r1 seq 72, head d8bc05749c32d013… (verified)
sales/nuramesh.yaml: valid
Tasks: 4 done
Ledger: run:r1 seq 72, head c9df6501ebf1cbf1… (verified)
support/nuramesh.yaml: valid
Tasks: 4 done
Ledger: run:r1 seq 72, head fd15abd55d80548b… (verified)
```

## 4. Report, from the ledger alone

```sh quickstart-templates
nuramesh report first-run --team dev-team | grep -E '^(Run|Tasks|Ledger)'
```

```text
Run first-run
Tasks: 3 done
Ledger: run:first-run seq 46, head 1cc85316aa5130c2… (verified)
```

(Drop the `grep` to see every gate and budget; add `--output json` for JSON.)

## 5. Verify the ledger

```sh quickstart-templates
nuramesh ledger verify --team dev-team
```

```text
org:dev-team: 3 events, head 7739aed20d44ab90… ok
run:first-run: 46 events, head 1cc85316aa5130c2… ok
2 streams, 0 broken
```

## 6. Export the ledger and check it without NuraMesh

`ledger export` writes a file (never over an existing one). `verifier/nuramesh_verify.py` is a
single standard-library Python file with no dependencies, so an auditor can check the chain on a
machine that has never seen NuraMesh. It lives in the repository's `verifier/` folder; copy that
folder next to your work. What the check does and does not prove: auditor-guide.md.

```sh quickstart-templates
nuramesh ledger export --team dev-team --out ledger.json
python3 verifier/nuramesh_verify.py ledger.json
```

```text
exported 49 events to ledger.json
check offline with: python3 verifier/nuramesh_verify.py ledger.json
mode: chain
org:dev-team: 3 events, head 7739aed20d44ab90..., chain ok
run:first-run: 46 events, head 1cc85316aa5130c2..., chain ok
RESULT: no problems found (mode: chain; see docs/auditor-guide.md for what that does not prove)
```

## 7. The model catalog and assignments (fake provider only)

The template teams use the built-in `fake` provider, so they have no catalog entries or connections
of their own; the catalog fills when someone connects a real provider through the API or the app
([model-governance.md](model-governance.md)). Reading the state is safe and free:

```sh quickstart-templates
nuramesh models catalog --team dev-team
nuramesh models assignments --team dev-team
nuramesh models check --team dev-team --data synthetic
```

```text
no catalog
SUBJECT      MODEL            FROM
agent=chief  -                none
agent=dana   fake/dev-1       role config
agent=theo   fake/dev-1       role config
agent=rhea   fake/reviewer-1  role config
agent=omar   fake/dev-1       role config
all models resolve for synthetic data
  agent=dana  fake/dev-1  -  local  -  local  book
  agent=theo  fake/dev-1  -  local  -  local  book
  agent=rhea  fake/reviewer-1  -  local  -  local  book
  agent=omar  fake/dev-1  -  local  -  local  book
```

`models check` is the "would a run start?" answer (`--data customer` applies the stricter customer-data
rules; the fake provider is local, so it passes too). `pause`, `retire`, `disable` and `disconnect` only restrict, so the CLI has them;
anything that widens what may run is the signed-in API or the app.

## Next

- demo.md: what the run proves, and the evals (`nuramesh eval run`).
- [model-governance.md](model-governance.md): the catalog, regions, approvals and connections.
- auditor-guide.md: what an outside checker can and cannot conclude.
