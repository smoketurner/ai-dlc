# Platform Architecture

An agentic SDLC platform built on AWS Bedrock AgentCore that connects to target GitHub repositories via a GitHub App and drives a single-PR-per-issue pipeline.

## High-Level Overview

ai-dlc is an automated software development lifecycle platform. A GitHub issue assigned to the bot (or a dashboard submission) produces exactly one implementation PR on the target repository. Eight agents collaborate through an event-driven state machine to triage, plan, implement, validate, and learn from each run.

The platform is a single deployment that serves many target repos. Target repos install the ai-dlc GitHub App; the platform then reads their code, opens PRs, and responds to webhooks on their behalf.

## Key Components

### Agents (8)

All agents ship as `linux/arm64` container images running on Bedrock AgentCore Runtime.

| Agent | Framework | Model |
|-------|-----------|-------|
| Triage | Strands | Claude Haiku 4.5 |
| Architect | Strands | Claude Opus 4.6 |
| Implementer | Claude Agent SDK | Claude Sonnet 4.6 |
| Reviewer | Strands | Claude Sonnet 4.6 |
| Tester | Strands | Claude Haiku 4.5 |
| Code-Critic | Strands | Claude Opus 4.6 |
| Proposer | Strands | Claude Opus 4.6 |
| Retrospector | Strands | Claude Haiku 4.5 |

### Lambdas (6)

| Lambda | Role |
|--------|------|
| `entry_adapter` | API Gateway target. Publishes `REQUEST.RECEIVED` to EventBridge. |
| `state_router` | SQS beacon consumer. Reads DDB event log, runs `decide()`, dispatches side-effects. Never writes state. |
| `event_projector` | EventBridge consumer. Sole writer of run state (EVENT + SUMMARY rows in DDB). Forwards to AgentCore Memory. |
| `artifact_tool` | AgentCore Gateway target for S3 + `MEMORY.md` operations. |
| `repo_helper` | AgentCore Gateway target for git/GitHub operations, including `get_check_state(pr_url)`. |
| `retrospector_dispatcher` | EventBridge rule target that invokes the Retrospector on terminal and PR-signal events. |

### Services

| Service | Role |
|---------|------|
| `services/dashboard/` | FastAPI + Jinja2 + Alpine.js (CDN, no JS build). Submission form, run tracking UI, webhook receiver. Deployed on API Gateway + Lambda with Cognito OIDC auth. |

### Packages

| Package | Role |
|---------|------|
| `packages/common/` | Shared library. Event envelopes, routing, AgentCore wrappers, gateway tools, DDB helpers, MEMORY.md parser, stack-profile reader/writer. |

### Terraform (IaC)

| Path | Role |
|------|------|
| `terraform/modules/` | Reusable modules (one per concern): agents, auth, ci_cd, dashboard, improvement, messaging, observability, pipeline, registry, state |
| `terraform/envs/dev/` | Dev environment composition |
| `terraform/bootstrap/` | One-time S3 + DDB state backend setup |

## Infrastructure

### SQS-Beacon + DynamoDB State Machine

The state machine is event-sourced. DynamoDB holds one table (`runs`) with:

- `EVENT#<event_id>` rows forming the timeline (one per event)
- A `SUMMARY` row per run carrying accumulators (tokens, cost, duration) and GSI keys

The DDB Stream pipes EVENT row INSERTs to an SQS queue (the "beacon"). The `state_router` consumes beacons, queries the full event log for the run, and calls `decide(events)` -- a pure function -- to determine the next side-effect.

### EventBridge

Custom bus with a schema registry. Every platform event is published here. The `event_projector` subscribes to all events. The `retrospector_dispatcher` subscribes to terminal + PR-signal events.

### S3 Artifacts Bucket

Stores plans (`runs/{run_id}/plan.md`), validator outputs (`runs/{run_id}/validation/{kind}-r{N}.md`), and stack profiles.

### API Gateway + Lambda

The dashboard and entry adapter run as Lambda functions behind API Gateway.

### Cognito OIDC Auth

Single user pool covering API Gateway. Bypassed locally with `AIDLC_AUTH=disabled`.

## The Two-Lambda Split

This is load-bearing architecture:

- **`event_projector`** -- the sole writer of run state. Receives every EventBridge event, atomically writes the EVENT timeline row + SUMMARY accumulator update in a DDB transaction (conditional on `attribute_not_exists(sk)` for idempotency). After commit, forwards the event to AgentCore Memory. The DDB Stream INSERT on the EVENT row generates the next SQS beacon via an EventBridge Pipe.

- **`state_router`** -- reads DDB state and dispatches side-effects. Never writes to DDB. Consumes the SQS beacon, queries all `EVENT#*` rows for the run, passes them to the pure `decide()` function, then executes the resulting action (invoke an agent, emit an event, or noop). Emits `*.DISPATCHED` marker events as idempotency proof before invoking agents.

This split keeps state-machine logic in one place and makes every transition observable as an EventBridge event. Re-delivery of beacons is safe because `decide()` is replay-safe.

## Memory Model

### AgentCore Memory

Cross-session semantic facts (per-user preferences, team-level signals) and session events (60-day TTL). Every projected event is forwarded to AgentCore Memory via `CreateEvent`.

### MEMORY.md

Repository-scoped context file (six fixed sections: Overview, Conventions, Decisions, Constraints, Glossary, Notes). Human-reviewed via PRs.

Sync is one-way: `MEMORY.md` content is read into AgentCore Memory on every session. The reverse only happens through agent-proposed PRs -- humans gate all writes to `MEMORY.md`.

### Nested MEMORY.md (Stripe Pattern)

Target repos may carry per-directory `MEMORY.md` files. The loader (`packages/common/src/common/memory_md.py`) walks each changed path's directory chain and unions the relevant files. Use the deepest scope that makes sense (e.g., a TypeScript-only convention in `src/web/MEMORY.md`, not the root).

### Skills System

Skills are packaged multi-step procedures in the `agentskills.io` layout:

- `.aidlc/skills/<slug>/SKILL.md` in target repos
- `.claude/skills/<slug>/SKILL.md` in the platform repo

`SKILL.md` has YAML frontmatter (`name` + `description`) and a body with the procedure. Agents load only frontmatter at preamble time (progressive disclosure via `common.memory.agent_skills_preamble`), then read the body on demand when the description matches the task.

Currently consumed by Architect and Implementer (agents with local repo checkouts).

## Key Directories

| Path | Role |
|------|------|
| `packages/common/` | Shared library: events, state, routing, AgentCore wrappers, gateway tools, DDB helpers, MEMORY.md parser |
| `agents/architect/` | Strands agent -- writes `plan.md` to S3 |
| `agents/code_critic/` | Strands agent -- adversarial review against the original issue |
| `agents/implementer/` | Claude Agent SDK agent -- opens the impl PR, handles revisions |
| `agents/reviewer/` | Strands agent -- code-reviews the impl PR (gating) |
| `agents/tester/` | Strands agent -- flags test gaps (advisory) |
| `agents/triage/` | Strands agent -- classifies issue-driven runs |
| `agents/proposer/` | Strands agent -- research-driven PRs |
| `agents/retrospector/` | Strands agent -- lesson extraction + consolidation |
| `lambdas/entry_adapter/` | API Gateway entry point |
| `lambdas/state_router/` | SQS beacon consumer + dispatcher |
| `lambdas/event_projector/` | EventBridge consumer + DDB writer |
| `lambdas/artifact_tool/` | AgentCore Gateway -- S3 + MEMORY.md ops |
| `lambdas/repo_helper/` | AgentCore Gateway -- git/GitHub ops |
| `lambdas/retrospector_dispatcher/` | EventBridge rule target for Retrospector |
| `services/dashboard/` | FastAPI submission/tracking UI |
| `terraform/modules/` | Reusable Terraform modules (one per concern) |
| `terraform/envs/dev/` | Dev environment composition |
| `terraform/bootstrap/` | One-time state backend setup |
| `docs/ADRs/` | Architectural Decision Records |
