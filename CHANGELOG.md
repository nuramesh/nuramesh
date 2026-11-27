# Changelog

All notable changes to NuraMesh are recorded in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project will follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) from 1.0.0. Until then NuraMesh is pre-1.0
and interfaces may change between releases.

## 0.1.0 (2026-11-27)

### Added

- The first public tree of the NuraMesh open core, licensed under the Apache License 2.0.
- The Python package `nuramesh`: the core domain model, the run engine with gates and independent
  verifiers, budgets with hard stops, default-deny policies, canon from Git, evals per role, and the
  append-only audit ledger with signed anchors.
- Stores for memory, SQLite and PostgreSQL.
- The LangGraph adapter.
- The `nuramesh` command and the REST API server, with sign-in on by default.
- The web app with the Workflow builder, and the TypeScript SDK.
- The independent ledger verifier.
- Example teams and the guides in the documentation folder.
