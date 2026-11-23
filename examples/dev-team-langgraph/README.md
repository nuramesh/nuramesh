# dev-team-langgraph

The [dev-team](../dev-team/) example with its workers written as LangGraph graphs. It runs on the built-in fake model: no API keys, no cost.

## What it does

The same three tasks (fix, test, deploy), the same agents and the same rules as `dev-team`. The difference: Dana, Theo and Omar each run as a compiled LangGraph graph through `nuramesh.adapters.langgraph`. NuraMesh stays the control layer:

- the graph's model calls go through `NuraMeshChatModel` (policy, price, budget, ledger);
- its actions ask `authorize` or `authorize_command` first;
- gates, not the graphs, decide whether a task is done.

## Agents and roles

Chief of Staff (delegates), Dana (developer), Theo (tester), Rhea (independent reviewer), Omar (DevOps): see the table in [dev-team](../dev-team/README.md).

## Gates, policies and budgets

Identical to `dev-team` (a test keeps the two `nuramesh.yaml` files in step): rule, test and independent-review gates on the fix; default-deny policies per role; 5.00 USD monthly budgets for Dana and Rhea; a human approval before the deploy.

## Run it

From the repository root. This example needs the `langgraph` extra:

```bash
uv sync --locked --extra local --extra langgraph
uv run nuramesh try examples/dev-team-langgraph --fake --approve-as human:demo --allow-unsandboxed --run-id first-run
uv run nuramesh ledger verify --team examples/dev-team-langgraph
```

`--allow-unsandboxed` is needed only when no container runtime (Podman or Docker) is found.

## What you will see

```
Tasks: 3 done
  [done] first-run-fix-add (dana, attempts 1): Fix the failing add() test in calc.py
      gate has-fix (rule): passed by gate:rule
      gate tests (test_command): passed by gate:test_command
      gate review (verifier): passed by agent:rhea
  ...
Spend: $0.003471 (2 calls)
Ledger: run:first-run seq 46, head 16d7ddc84d50246f… (verified)
```

and from `ledger verify`:

```
run:first-run: 46 events, head 16d7ddc84d50246f… ok
2 streams, 0 broken
```

The ledger head hash is different on every run, so yours will not match the one above.
