# Changelog

All notable changes to NuraMesh are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
[Semantic Versioning](https://semver.org/).

## 0.1.0 (unreleased)

The first release of the open core.

### Added

- Verified work: a task is done only when its gates passed, with the gate results recorded as evidence.
- Policies with default deny: every action is allowed, asked or denied by a role's rules; anything no rule allows is denied. A person can approve an action that needs approval.
- Hard budgets: spend is checked before each model call and a limit stops the work.
- Tamper-evident ledger: every step is hash-chained, `nuramesh ledger verify` checks the chains, and `nuramesh ledger export` writes the ledger out for an offline check.
- Anchors: ledger heads can be signed and committed to a Git repository (`nuramesh anchor`).
- Earned autonomy: proposals, grants, ceilings and settings (`nuramesh autonomy`), based on evals and evidence.
- Kill switch: stop a run (`nuramesh stop`) or halt the whole organization.
- Model governance: a model catalog, assignments and connection tests (`nuramesh models`).
- Command line: `nuramesh doctor`, `init`, `validate`, `try`, `serve`, `eval`, `roles`, `auth`, `ledger` and `anchor`, among others, with `--fake` for a run on the built-in fake model that needs no keys and costs nothing.
- Team templates for `nuramesh init`: dev team, code review, review board, DevOps, QA, kanban, scrum, sales and support.
- A team lives in one readable file, `nuramesh.yaml`.
- API server (`nuramesh serve`) with PostgreSQL support, and SQLite for a laptop.
- No telemetry and no license check over the network.
- Examples: `dev-team` and `dev-team-langgraph`, plus a small demo repository the sample tests run against (see `examples/`).

Works with:

- Agent frameworks: the NuraMesh built-in engine and LangGraph.
- Models: Azure OpenAI, Anthropic, OpenAI, Gemini, Mistral and Groq, and local Ollama, vLLM and LM Studio.

### Not in this release

Coming soon: OpenAI Agents SDK, Claude Agent SDK, LangChain, CrewAI, AWS Bedrock, Google Vertex AI, Azure AI Foundry (non-OpenAI models) and Docker Compose.
