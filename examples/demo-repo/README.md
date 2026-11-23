# Demo repository

A tiny project with one failing test (`add` subtracts). The `examples/dev-team` run fixes it.
The original files here are never changed: every run works on copies.

To see the bug yourself, from the repository root:

```bash
python3 -m unittest discover -s examples/demo-repo
```

It prints `FAILED (failures=2)` and exits with code 1: that is expected. To watch a team fix it, run [dev-team](../dev-team/) or [dev-team-langgraph](../dev-team-langgraph/).
