# ai-dlc

An agentic Software Development Lifecycle (SDLC) platform on AWS Bedrock AgentCore.

A Step Functions state machine drives a deterministic pipeline (Spec → Architecture → Implementation → QA → Deploy → Doc) where each stage is an AgentCore-hosted specialist agent. Mandatory human-in-the-loop gates run via GitHub PR reviews. Memory is hybrid: AgentCore Memory for cross-session semantic facts, `MEMORY.md` files for repository-scoped context.

## Status

Initial scaffold. The full architecture is described in [`docs/aws-agent-architecture-guide.md`](docs/aws-agent-architecture-guide.md). The project manifest lives in [`CLAUDE.md`](CLAUDE.md).

## Quick start

```bash
uv sync                                  # resolve and install the workspace
uv run ruff check                         # lint
uv run ty check                           # type-check
uv run pytest -q                          # tests
```

## Layout

- `packages/common/` — shared models, event schemas, hybrid-memory utility.
- `agents/architect/` — Strands-based architect agent (Opus 4.7).
- `agents/implementer/` — Claude Agent SDK-based implementer (Sonnet 4.6).
- `lambdas/` — AWS Lambda functions for entry, HITL, projection, and gateway tools.
- `services/dashboard/` — FastAPI + Jinja2 + Alpine.js pipeline UI.
- `terraform/` — all infrastructure (modules, environments, bootstrap).

## License

TBD.
