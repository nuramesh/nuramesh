# Quickstart: your first verified team in 10 minutes (every template, the ledger check and the model catalog)

This extends [quickstart.md](quickstart.md) (install the wheel first, steps 1 to 3 there). It uses
only what is on `main`: the shipped templates, `validate`, `try --fake`, `report`,
`ledger verify`, `ledger export` with the standalone verifier, and the model commands. Everything
runs on the **fake model**: no keys, no cost, no network.

Every command below is run by an internal test in a fresh folder, and each sample
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
| `research-report` | a researcher reads the bundled sources, a writer drafts a cited report, and a human approves before it counts as published | an independent fact checker verifies every claim against the sources |
| `document-extraction` | an extractor reads the bundled sample invoices and an exporter writes a CSV after a human approves | an independent invoice checker compares the extracted fields with the documents |
| `data-analysis` | an analyst answers a question from the bundled sample database and a summarizer writes it up | an independent number checker checks the summary against the recomputed figures |
| `incident-triage` | a triager rates the bundled sample alert, a responder proposes the fix (a human approves any production change) and a writer drafts the postmortem | an independent incident reviewer checks the triage and the postmortem |
| `multi-framework-org` | one organization with two teams and one ledger: a native research agent and a LangGraph drafting agent, with central policies and budgets | a central reviewer on a different model checks both teams' work |

```sh quickstart-templates
nuramesh init dev-team --template dev-team
```

```text
created dev-team from the 'dev-team' template. Next:
  nuramesh validate dev-team/nuramesh.yaml
  nuramesh try dev-team --fake --approve-as human:you --allow-unsandboxed --run-id first-run
    (no container runtime (Podman or Docker) answered here, so the tests run on
    this machine without isolation: --allow-unsandboxed prints a warning and
    records it in the ledger; it is never a default. Start Podman or Docker to
    leave it out.)
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
  [done] first-run-fix-add (developer, attempts 1): Fix the failing add() test in calc.py
      gate has-fix (rule): passed by gate:rule
      gate tests (test_command): passed by gate:test_command
      gate review (verifier): passed by agent:reviewer
  [done] first-run-test-suite (tester, attempts 1): Run the whole test suite on the fix
      gate green (rule): passed by gate:rule
  [done] first-run-deploy (devops, attempts 1): Deploy the fix to staging
      gate deploy-plan (rule): passed by gate:rule
      gate approve-deploy (human): passed by human:you
Budgets:
  developer-monthly (agent developer): spent 0.001335 of 5.00, reserved 0.000000
  devops-monthly (agent devops): spent 0 of 5.00, reserved 0
  rb-486e135495bd (run first-run): spent 0.003471 of 2.00, reserved 0.000000
  reviewer-monthly (agent reviewer): spent 0.002136 of 5.00, reserved 0.000000
Spend: $0.003471 (2 calls)
Ledger: run:first-run seq …, head 0e0e09a53cda237a… (verified)
```

Numbers in your run may differ: the sequence number and the event counts change from run to run and are shown as `…`.

`--approve-as` is a scripted approval so the run finishes alone (the ledger records that);
`--allow-unsandboxed` is local only, see [quickstart.md](quickstart.md) step 4. Nothing is done
until its gates have recorded results, and the reviewer never sees the developer's reasoning.

The other templates run the same way. Here each is created, validated and run, and only the
summary lines are shown:

```sh quickstart-templates
for t in kanban review-board scrum code-review devops qa sales support research-report document-extraction data-analysis incident-triage multi-framework-org; do
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
research-report/nuramesh.yaml: valid
Tasks: 3 done
Ledger: run:r1 seq 59, head 0c3ce17ae258e81b… (verified)
document-extraction/nuramesh.yaml: valid
Tasks: 2 done
Ledger: run:r1 seq 41, head c575f2018d1c8923… (verified)
data-analysis/nuramesh.yaml: valid
Tasks: 2 done
Ledger: run:r1 seq 44, head df1864c9a3a1398c… (verified)
incident-triage/nuramesh.yaml: valid
Tasks: 3 done
Ledger: run:r1 seq 61, head 1b1f4c9a059bc30f… (verified)
multi-framework-org/nuramesh.yaml: valid
Tasks: 2 done
Ledger: run:r1 seq 48, head 93daede3f97b25f6… (verified)
```

## 4. Report, from the ledger alone

```sh quickstart-templates
nuramesh report first-run --team dev-team | grep -E '^(Run|Tasks|Ledger)'
```

```text
Run first-run
Tasks: 3 done
Ledger: run:first-run seq …, head 0e0e09a53cda237a… (verified)
```

(Drop the `grep` to see every gate and budget; add `--output json` for JSON.)

## 5. Verify the ledger

```sh quickstart-templates
nuramesh ledger verify --team dev-team
```

```text
org:dev-team: … events, head 65ea1778e19fbcf9… ok
run:first-run: … events, head 0e0e09a53cda237a… ok
2 streams, 0 broken
```

## 6. Export the ledger and check it without NuraMesh

`ledger export` writes a file (never over an existing one). `verifier/nuramesh_verify.py` is a
single standard-library Python file with no dependencies, so an auditor can check the chain on a
machine that has never seen NuraMesh. It lives in the repository's `verifier/` folder; copy that
folder next to your work.

```sh quickstart-templates
nuramesh ledger export --team dev-team --out ledger.json
python3 verifier/nuramesh_verify.py ledger.json
```

```text
exported … events to ledger.json
check offline with: python3 verifier/nuramesh_verify.py ledger.json
mode: chain
org:dev-team: … events, head 65ea1778e19fbcf9..., chain ok
run:first-run: … events, head 0e0e09a53cda237a..., chain ok
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
SUBJECT          MODEL            FROM
agent=chief      -                none
agent=developer  fake/dev-1       role config
agent=tester     fake/dev-1       role config
agent=reviewer   fake/reviewer-1  role config
agent=devops     fake/dev-1       role config
all models resolve for synthetic data
  agent=developer  fake/dev-1  -  local  -  local  book
  agent=tester  fake/dev-1  -  local  -  local  book
  agent=reviewer  fake/reviewer-1  -  local  -  local  book
  agent=devops  fake/dev-1  -  local  -  local  book
```

`models check` is the "would a run start?" answer (`--data customer` applies the stricter customer-data
rules; the fake provider is local, so it passes too). `pause`, `retire`, `disable` and `disconnect` only restrict, so the CLI has them;
anything that widens what may run is the signed-in API or the app.

## Next

- [model-governance.md](model-governance.md): the catalog, regions, approvals and connections.
