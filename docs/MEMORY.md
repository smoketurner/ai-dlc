# Project Memory

This file is the human-reviewed source of truth for project-scoped context that ai-dlc agents read at the start of every session and propose updates to via PR. Cross-session semantic facts (per-user preferences, team-level signals) live in AgentCore Memory, not here.

Six sections, in order. Agents fail-fast on unknown headers.

## Overview

ai-dlc is the agentic SDLC platform itself. Two agents (Architect on Strands, Implementer on Claude Agent SDK) produce ADRs and code PRs through a Step Functions pipeline gated by GitHub PR reviews.

## Conventions

- Python 3.14, Astral toolchain only (`uv`, `ruff`, `ty`).
- All agents ship as `linux/arm64` container images on AgentCore Runtime.
- Step Functions uses the native `aws-sdk:bedrockagentcore:invokeAgentRuntime` integration — no `runtime_invoker` Lambda hop.
- Pin every dependency to an exact version. Pin every GitHub Action to a SHA with a version comment.
- Replace, don't deprecate: when a new implementation supersedes an old one, remove the old one entirely.

## Decisions

ADR bullets land here as the Architect agent commits them. Format: `- [ADR-NNNN](docs/ADRs/NNNN-slug.md): one-line summary`.

## Constraints

- AgentCore Runtime allows only Python 3.10–3.13 in `code_configuration`; we use `container_configuration` with our own 3.14 image.
- AgentCore Runtime requires `linux/arm64` images.
- Step Functions state size limit: 1 MiB. Large agent outputs go to S3; only the key returns through the workflow.

## Glossary

- **ADR** — Architectural Decision Record. Written by the Architect, committed under `docs/ADRs/`.
- **HITL** — Human-in-the-loop. Mandatory PR-review gates between SDLC stages.
- **Run** — One execution of the SDLC pipeline. Identified by a UUID7 `run_id`.

## Notes

(Free-form. Append-only. The Implementer pushes incidental observations here when they don't yet rise to an ADR.)
