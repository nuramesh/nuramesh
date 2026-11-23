# Examples

Ready-made agent teams. Each runs on the built-in fake model: no API keys, no cost.

| Example | What it shows | Framework | Run |
|---|---|---|---|
| [dev-team](dev-team/) | A five-agent dev team that fixes a failing test, with rule, test and independent-reviewer gates, hard budgets, per-role policies and a human approval before deploy | NuraMesh built-in engine | [Run it](dev-team/) |
| [dev-team-langgraph](dev-team-langgraph/) | The same team with its workers written as LangGraph graphs; policies, budgets, gates and the ledger stay NuraMesh's | LangGraph | [Run it](dev-team-langgraph/) |
| [demo-repo](demo-repo/) | The small project with one failing test that both teams above fix; not a team itself | None (plain Python) | [See the bug](demo-repo/) |

Install first (see the [Quickstart guide](https://docs.nuramesh.com/quickstart/)): `uv sync --locked --extra local`. The LangGraph example also needs `--extra langgraph`.

`--allow-unsandboxed` is needed only when no container runtime (Podman or Docker) is found. The tests then run directly on your machine, so try it in a throwaway folder.

After any run, `uv run nuramesh ledger verify --team examples/<name>` proves the ledger was not changed afterward.
