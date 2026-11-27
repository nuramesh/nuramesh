<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo-light.svg" alt="NuraMesh" width="320">
  </picture>
</p>

<h3 align="center">The Control Plane for AI Agents.</h3>

<p align="center">
  <a href="https://github.com/nuramesh/nuramesh/releases"><img alt="Release v0.1.0" src="https://img.shields.io/badge/release-v0.1.0-71D9A0?labelColor=141833"></a>
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-71D9A0?labelColor=141833"></a>
  <a href="https://github.com/nuramesh/nuramesh/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/nuramesh/nuramesh/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://www.linkedin.com/company/nuramesh"><img alt="Follow @NuraMesh on LinkedIn" src="https://img.shields.io/badge/-%40NuraMesh-71D9A0?logo=data%3Aimage%2Fpng%3Bbase64%2CiVBORw0KGgoAAAANSUhEUgAAACgAAAAiCAYAAAAtZZsLAAADGElEQVR42u2Yu2sVQRTGf7PZmGBCFCIpNIWIBhSDNtHCJ6IGX4gggo1WltpaiLWVoH%2BBldgogqBBLQQJiJUo%2BADBwkcTJEp8BJN797M5I8dhb7i5udkqB4bZnTk78813zpw5s0HSeuAGsB0IVqoQAbPAI%2BAsMOXanZY0LmlWUqHqpS5pRtJVSUjKrf5XMmMuK2GuAGpWLyaLAdjh5vxPMqM5KwEXXPtigowYSiUvYS76wHvgDbAO2OxWuxgS5kKfMifgOjAMnAC2ApesvaBqkTTtHLaQ9M45aS4p2PNT06m1cZPUbMxxmyMr2ySevTrwwpjNbZPkVp47hiuTPPGDAGx0puw0QDUzeVY1QM9gh9XDwEUDOWvgzgD7E73KGcQxdAU4CLwEhoDDiV49YTI44LEvjqWkf0EA42QFsAvY6UJPlujEtsKFCbk%2Br5f2LwhgHRgDfrq2LuAQ0G3vT4BJB7ATOG4A7gP3gE%2FAMmCtWeNISyBdmIln8S9Jfel2l%2FTFdApJm5K%2BHkkTkk6WfBfLqKRJCy1FK2HGm2%2BlsdNpdV%2By6hXW3mV1P3AauG1tXe7b3J4fAucSF2jJxNHMRRIfvdSSZOKjFYA%2FyWJrVufAHdt4W5o9lbI2n6U9wFHgFLCmwaYZM3DFQhhsBdxq4LGFJYBvwD7gtSNC9t5SoG5VOmziC8AGc4e6%2BfH5JCYCTNh7VhWD0VTbnK9FGUryS4Dp%2BWyUrE1ZMUBvSTDvLrlnqGoTV5YsLAFsJRtaYrDd%2BaA3Q%2BbM0VHyXewPSRhJg3jmiuZLSt4gbHxPjqOpJDxMWd%2BMayu729ZKjrUfTVi1iAvKG4A%2BBnx1yt0upmFH2KAxGC9S%2FQmLwdpGHZN1u2PTIFj7DLwACJKmk8kb%2FUnISgZqxp%2BLErMHlzUF4Jll78uAEWC5sf8qnyOya46bvxqsvhm9svHi%2BwgwYEB%2FA3tiQlkkxa80OCYa9Qf358EXlejRYL54zf0A7DWAnzPg5hyTLnaJFrzl%2FL8XeGB7YCBI6gYuA7tdOAkV%2FLyMO%2F8ucM18eBVwwC5cg8Dbv7weegC4c5JBAAAAAElFTkSuQmCC&labelColor=141833"></a>
  <a href="https://x.com/NuraMeshAI"><img alt="Follow @NuraMeshAI on X" src="https://img.shields.io/badge/-%40NuraMeshAI-71D9A0?logo=x&logoColor=white&labelColor=141833"></a>
</p>

<div align="center">

[Docs](https://docs.nuramesh.com) · [Quickstart](https://docs.nuramesh.com/get-started/quickstart/) · [Examples](examples/) · [Discussions](https://github.com/nuramesh/nuramesh/discussions) · [Security](SECURITY.md)

</div>

NuraMesh is an open-core platform for agent teams whose work is verified. You define an organization of
agents as a graph. NuraMesh adds verification gates, evals per role, budgets with hard stops, a shared
canon from Git, an append-only ledger of every action, and default-deny autonomy policies. It builds on
existing frameworks (LangGraph first) and does not replace them. Status: pre-1.0, interfaces may change.

![NuraMesh architecture: the command line, API server, web app and SDKs drive the trust layer, which checks your LangGraph agent team with policies, budgets and gates and records every action in the audit ledger](docs/assets/architecture.svg)

![The NuraMesh web app: the Workflow builder with the Dev team template loaded (sample data)](docs/assets/app-builder.png)

*The web app's Workflow builder with the Dev team template loaded (a screenshot of the app on its
built-in sample data).*

## Why NuraMesh

Agent frameworks make it easy to get agents to act. They do not tell you whether the work is right, what
it cost, or who allowed it. NuraMesh is the control plane around the framework you already use. One task,
one loop:

**1. Connect:** your frameworks, models and tools.
- **LangGraph adapter:** run your LangGraph agents under NuraMesh control.
- **Model providers:** Azure OpenAI, Anthropic and any OpenAI-compatible endpoint, including local models.
- **Model catalog:** admins decide which models may be used, and by which agent.
- **Python SDK and CLI:** define teams, checks, budgets and rules in code, and run them on your laptop.
- **REST and WebSocket API**, and **OpenTelemetry export** to your own monitoring stack.

**2. Build:** design your agent team.
- **Workflow builder:** draw your team on a canvas, with its checks and human approvers.
- **Team templates:** start from a ready team: dev team, code review, support, sales and more.
- **YAML/JSON export:** export the team as a file, or import one; it stays in step with the canvas.

**3. Govern:** what an agent may do, spend and decide.
- **Autonomy policies:** allow, ask or deny for each role and action. Denied unless you allow it.
- **Budgets:** spend is checked before each model call. Over budget is refused.
- **Kill switch:** stop an organization, a role or a rule. Only a person can restart it.
- **Agent identity:** each agent has its own credential, and you can revoke it.
- **Output caps** per role, and **cost views** per verified result.
- **Accounts and sign-in:** sign-in, invites and two-step verification; owner, approver and viewer roles.

**4. Run:** start a run and follow it.
- **Live run view:** watch each agent work, see the cost so far, and stop the run.
- **Chief of Staff:** give the team a goal; it comes back as checked work and a report.
- **Dashboards:** checks, costs and waiting approvals at a glance.
- **Durable runs** on the light engine, and **durable storage**: PostgreSQL in production, a single file on
  your laptop.

**5. Approve:** a person signs off where the rules say so.
- **Approvals:** each approval works once, for one action, and goes on the record.

**6. Verify:** work is checked before it counts.
- **Gates:** nothing counts as done until its checks pass.
- **Independent verifier:** a separate reviewer that never sees the worker's reasoning.
- **Sandbox:** test commands run in a locked-down container.
- **Evals per role:** agents pass their evals before they take work.
- **Verifier calibration:** know how often each reviewer catches bad work.

**7. Record:** evidence of everything that happened.
- **Audit ledger:** a tamper-evident record of every action, cost, check and approval.
- **Offline ledger check:** check the record yourself, offline, without trusting our software.
- **Git anchoring:** signed checkpoints of the record, kept in a Git repository you control.
- **Task audit view** and **ledger export** for an auditor.
- **Content store and retention:** content is kept only if you opt in, encrypted, with a retention limit.
- **Canon from Git:** one pinned source of truth for every agent and reviewer.

**Your data stays yours:** self-hosted, and no telemetry unless you turn on export.

## Quickstart

You need Python 3.12 or newer. In a virtual environment and a throwaway folder:

```sh
pip install "nuramesh[local]"
nuramesh init my-team --template dev-team
nuramesh validate my-team/nuramesh.yaml
nuramesh try my-team --fake --approve-as human:you --allow-unsandboxed --run-id first-run
nuramesh ledger verify --team my-team
```

The run uses the built-in fake model: no keys, no cost. The [Quickstart](https://docs.nuramesh.com/get-started/quickstart/)
explains each step, including how to run the sample team's tests in a container. Other install options
(`server`, `postgres`, `langgraph`, `otel` and more): see [Install](https://docs.nuramesh.com/get-started/install/).

## Resources

- [Documentation](https://docs.nuramesh.com) and [Install options](https://docs.nuramesh.com/get-started/install/)
- [Templates](https://docs.nuramesh.com/get-started/templates/) and [examples](examples/)
- [Model governance](https://docs.nuramesh.com/concepts/model-governance/)
- [Command line](https://docs.nuramesh.com/reference/cli/) and [REST API](https://docs.nuramesh.com/reference/api/)
- [Environment variables](https://docs.nuramesh.com/reference/environment/) and [Telemetry](https://docs.nuramesh.com/guides/telemetry/)
- [Self-hosting preview](https://docs.nuramesh.com/guides/self-hosting/) (coming soon, not yet supported)
- [Contributing](CONTRIBUTING.md), [Code of conduct](CODE_OF_CONDUCT.md) and [Discussions](https://github.com/nuramesh/nuramesh/discussions)
