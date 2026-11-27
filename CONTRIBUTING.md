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
