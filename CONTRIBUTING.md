# Contributing to NuraMesh

Thanks for looking. NuraMesh is the open core of a platform for agent teams whose work is verified.
How outside contributions are handled is set by the maintainers; open an issue to discuss a change
before you spend time on it. Report a security problem as `SECURITY.md` describes, never in an issue.

## Set up

```
uv sync --locked --all-extras
```

## Before you send a change

- Start with a failing test that shows the problem, then make it pass.
- Run the checks: `uv run ruff check . --fix && uv run ruff format .`, `uv run mypy` and
  `uv run pytest -q` (the web app has its own `pnpm` checks under `apps/app`).
- Tests use the fake model and never call a real model API. Test data is never key-shaped: generate
  a secret when the test runs, or use an obviously fake phrase.
- Keep the open core small: `nuramesh.core` imports only the standard library and pydantic.

## Licence

NuraMesh is licensed under the Apache License 2.0 (`LICENSE`). By sending a change you agree that
it is licensed under the same terms.
