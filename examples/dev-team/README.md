# dev-team

A small dev team that fixes a failing test in [demo-repo](../demo-repo/). It runs on the built-in fake model: no API keys, no cost. Budgets, prices, policies, gates and the ledger are all real.

## What it does

The Chief of Staff delegates three tasks in order:

1. **Fix** (Dana): produce a corrected `calc.py`.
2. **Test** (Theo): run the whole test suite on the fix.
3. **Deploy** (Omar): prepare a staging deploy; a human approves it.

## Agents and roles

| Agent | Role | Does |
|---|---|---|
| Chief of Staff | Chief of Staff | delegates the tasks |
| Dana | Developer | writes the fix |
| Theo | Tester | runs the tests |
| Rhea | Independent reviewer (verifier) | reviews the fix; sees only the task, the definition of done and the output, never Dana's reasoning |
| Omar | DevOps engineer | prepares the deploy |

## Gates, policies and budgets

- **Gates** (cheapest first) on the fix: a rule (the fix is there), the tests (`python -m unittest`), then the independent review.
- **Policies** (default deny): the developer may call `fake/*` models and write `*.py` files; the tester may run only `python -m unittest`; DevOps must ask a human before any merge.
- **Budgets:** Dana and Rhea have a monthly limit of 5.00 USD each; the run has its own limit.
- **Evals:** the `dev-basics` suite is defined for the developer role.

The team is one file: [nuramesh.yaml](nuramesh.yaml). `team.py` holds the scripted fake answers. `real-anthropic.yaml` and `real-azure-verifier.yaml` are example configs for real models (not used by the commands here).

## Run it

From the repository root, after `uv sync --locked --extra local`:

```bash
uv run nuramesh try examples/dev-team --fake --approve-as human:demo --allow-unsandboxed --run-id first-run
uv run nuramesh ledger verify --team examples/dev-team
```

`--allow-unsandboxed` is needed only when no container runtime (Podman or Docker) is found.

## What you will see

```
Run first-run
Tasks: 3 done
  [done] first-run-fix-add (dana, attempts 1): Fix the failing add() test in calc.py
      gate has-fix (rule): passed by gate:rule
      gate tests (test_command): passed by gate:test_command
      gate review (verifier): passed by agent:rhea
  [done] first-run-test-suite (theo, attempts 1): Run the whole test suite on the fix
      gate green (rule): passed by gate:rule
  [done] first-run-deploy (omar, attempts 1): Deploy the fix to staging
      gate approve-deploy (human): passed by human:demo
Spend: $0.003471 (2 calls)
Ledger: run:first-run seq 46, head 2a02953083dbe14b… (verified)
```

and from `ledger verify`:

```
run:first-run: 46 events, head 2a02953083dbe14b… ok
2 streams, 0 broken
```

The ledger head hash is different on every run, so yours will not match the one above.
