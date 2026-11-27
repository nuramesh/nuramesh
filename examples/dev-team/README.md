# dev-team

A small dev team that fixes a failing test in [demo-repo](../demo-repo/). It runs on the built-in fake model: no API keys, no cost. Budgets, prices, policies, gates and the ledger are all real.

## What it does

The Chief of Staff delegates three tasks in order:

1. **Fix** (developer): produce a corrected `calc.py`.
2. **Test** (tester): run the whole test suite on the fix.
3. **Deploy** (devops): prepare a staging deploy; a human approves it.

## Agents and roles

| Agent id | Role | Does |
|---|---|---|
| `chief` | Chief of Staff | delegates the tasks |
| `developer` | Developer | writes the fix |
| `tester` | Tester | runs the tests |
| `reviewer` | Independent reviewer (verifier) | reviews the fix; sees only the task, the definition of done and the output, never the developer's reasoning |
| `devops` | DevOps engineer | prepares the deploy |

## Gates, policies and budgets

- **Gates** (cheapest first) on the fix: a rule (the fix is there), the tests (`python -m unittest`), then the independent review. The deploy has a rule gate (it targets staging) and a human approval.
- **Policies** (default deny): the developer may call `fake/*` models and write `*.py` files; the tester may run only `python -m unittest`; DevOps must ask a human before any merge.
- **Budgets:** the developer, the reviewer and DevOps have a monthly limit of 5.00 USD each; the run has its own limit.
- **Evals:** the `dev-basics` suite is defined for the developer role.

The team is one file: [nuramesh.yaml](nuramesh.yaml). `team.py` holds the plan, the gates and the scripted fake answers. `real-anthropic.yaml` and `real-azure-verifier.yaml` are example configs for real models (not used by the commands here).

## Run it

From the repository root, after `pip install "nuramesh[local]"`:

```bash
nuramesh try examples/dev-team --fake --approve-as human:demo --allow-unsandboxed --run-id first-run
nuramesh ledger verify --team examples/dev-team
```

`--allow-unsandboxed` is needed only when no container runtime (Podman or Docker) is found.

## What you will see

```
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
      gate approve-deploy (human): passed by human:demo
Budgets:
  developer-monthly (agent developer): spent 0.001335 of 5.00, reserved 0.000000
  devops-monthly (agent devops): spent 0 of 5.00, reserved 0
  rb-486e135495bd (run first-run): spent 0.003471 of 2.00, reserved 0.000000
  reviewer-monthly (agent reviewer): spent 0.002136 of 5.00, reserved 0.000000
Spend: $0.003471 (2 calls)
Ledger: run:first-run seq 48, head 53fd2a4039bbec2f… (verified)
```

and from `ledger verify`:

```
run:first-run: 48 events, head 53fd2a4039bbec2f… ok
2 streams, 0 broken
```

The ledger head hash and the generated budget id are different on every run, so yours will not match the ones above.
