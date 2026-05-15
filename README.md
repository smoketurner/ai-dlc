# ai-dlc

An agentic Software Development Lifecycle (SDLC) platform on AWS Bedrock AgentCore.

Seven AgentCore-hosted specialist agents (Triage → Architect → Implementer → Reviewer + Tester + Code-Critic against the impl PR, plus Proposer for research-mode runs and Retrospector firing on terminal events) coordinated by an SQS-beacon + DynamoDB-state machine. One issue produces one impl PR; humans gate merge. Memory is hybrid: AgentCore Memory for cross-session semantic facts, `MEMORY.md` files for repository-scoped context.

## Status

Initial scaffold. The project manifest lives in [`AGENTS.md`](AGENTS.md).

Adopting on your own repo? See [`ADOPTING.md`](ADOPTING.md).

## Quick start

```bash
uv sync                                  # resolve and install the workspace
uv run ruff check                         # lint
uv run ty check                           # type-check
uv run pytest -q                          # tests
```

## Layout

- `packages/common/` — shared models, event schemas, state machine, hybrid-memory utility.
- `agents/` — eight AgentCore Runtime workers (Architect, Implementer, Reviewer, Tester, Code-Critic, Triage, Proposer, Retrospector) on Strands Agents + Claude Agent SDK.
- `lambdas/` — entry adapter, state router (dispatch), event projector (state writer), gateway tools (artifact + repo helper), telemetry.
- `services/dashboard/` — FastAPI + Jinja2 + Alpine.js pipeline UI.
- `terraform/` — all infrastructure (modules, environments, bootstrap).

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
