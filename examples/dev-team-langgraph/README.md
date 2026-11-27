# dev-team-langgraph

The [dev-team](../dev-team/) example with its workers written as LangGraph graphs. It runs on the built-in fake model: no API keys, no cost.

## What it does

The same three tasks (fix, test, deploy), the same agents and the same rules as `dev-team`. The difference: the developer, the tester and DevOps each run as a compiled LangGraph graph through `nuramesh.adapters.langgraph`. NuraMesh stays the control layer:

- the graph's model calls go through `NuraMeshChatModel` (policy, price, budget, ledger);
- its actions ask `authorize` or `authorize_command` first;
- gates, not the graphs, decide whether a task is done.

## Agents and roles

Chief of Staff (`chief`, delegates), `developer`, `tester`, `reviewer` (independent reviewer) and `devops`: see the table in [dev-team](../dev-team/README.md).

## Gates, policies and budgets

Identical to `dev-team` (a test keeps the two `nuramesh.yaml` files in step): rule, test and independent-review gates on the fix; default-deny policies per role; 5.00 USD monthly budgets for the developer, the reviewer and DevOps; a human approval before the deploy.

## Run it

This example needs the `langgraph` extra. Run it from the repository root:

```bash
pip install "nuramesh[local,langgraph]"
nuramesh try examples/dev-team-langgraph --fake --approve-as human:demo --allow-unsandboxed --run-id first-run
nuramesh ledger verify --team examples/dev-team-langgraph
```

`--allow-unsandboxed` is needed only when no container runtime (Podman or Docker) is found.

## What you will see

The same output as [dev-team](../dev-team/README.md#what-you-will-see): three tasks done, each with its gates passed, then the spend and the ledger line (`verified`). Then `ledger verify` ends with `2 streams, 0 broken`. The ledger head hash is different on every run.
