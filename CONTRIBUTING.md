# Contributing to NuraMesh

Thanks for looking. NuraMesh is the open core of a platform for agent teams whose work is verified.
We welcome bug reports and discussion in issues. Please open an issue before sending a pull request;
we review pull requests when we can and may decline changes that do not fit the project's scope, with
no promise of a response time. Please follow `CODE_OF_CONDUCT.md`. Report a security problem as `SECURITY.md` describes, never in an issue.

## Set up

```
uv sync --locked --all-extras
```

## Before you send a change

- Say how to reproduce the problem and how you checked that your change fixes it.
- Run the checks: `uv run ruff check . --fix && uv run ruff format .` and `uv run mypy` (the web
  app has its own `pnpm` checks under `apps/app`).
- The public repository ships the tests of the core, config, verifier and telemetry packages in its tests
  folder; the rest of the suite is not part of this repository. Our tests use
  the fake model and never call a real model API, and test data is never key-shaped.
- Keep the open core small: `nuramesh.core` imports only the standard library and pydantic.

## Licence

NuraMesh is licensed under the Apache License 2.0 (`LICENSE`). By submitting a contribution you agree
it is licensed under the Apache License 2.0 (section 5 of the licence), and you confirm you have the
right to submit it.

## Repository layout

Where each part of NuraMesh lives in this repository:

| Part | Where | What it does |
| --- | --- | --- |
| Core | [`src/nuramesh/core/`](src/nuramesh/core/) | The domain model: organization graph, tasks, gates, budgets, policies, ledger, money. Imports only the standard library and pydantic. |
| Engine | [`src/nuramesh/engine/`](src/nuramesh/engine/) | Runs a team: gates and verifiers, budgets, policy checks, canon, evals, anchors, sandbox. |
| Stores | [`src/nuramesh/stores/`](src/nuramesh/stores/) | Memory, SQLite and PostgreSQL behind one interface. |
| Adapters | [`src/nuramesh/adapters/`](src/nuramesh/adapters/) | Thin framework adapters. LangGraph is the first. |
| Server | [`src/nuramesh/server/`](src/nuramesh/server/) | The REST API. Sign-in is on by default. |
| Command line | [`src/nuramesh/cli.py`](src/nuramesh/cli.py) | The `nuramesh` command. |
| Web app | [`apps/app/`](apps/app/) | The web app with the Workflow builder and approvals. |
| TypeScript SDK | [`apps/sdk-ts/`](apps/sdk-ts/) | A typed client for the API. |
| Ledger verifier | [`verifier/`](verifier/) | One standard-library file that checks a ledger export against signed anchors, sharing no code with NuraMesh. |
