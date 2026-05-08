# ai-dlc — Project manifest

An agentic SDLC platform built on AWS Bedrock AgentCore.

## Tech stack

- **Python 3.14** with the Astral toolchain (`uv` workspace, `ruff`, `ty`).
- **Agents**: Strands Agents (Architect, Critic, Reviewer, Tester, Triage, Proposer) and Claude Agent SDK (Implementer), all shipped as `linux/arm64` containers on Bedrock AgentCore Runtime.
- **Models**: Architect / Critic → Claude Opus 4.7. Implementer / Reviewer → Claude Sonnet 4.6. Tester / Triage / memory consolidation → Claude Haiku 4.5.
- **Orchestration**: SQS-beacon + DynamoDB-state machine driven by a single `state_router` Lambda. The `event_projector` Lambda is the only writer of run/task state.
- **Eventing**: Amazon EventBridge (custom bus + schema registry), DynamoDB streams.
- **Memory**: AgentCore Memory (semantic + summarization strategies) plus per-project `MEMORY.md` files in the AgentCore Runtime persistent filesystem (snapshotted to S3).
- **Dashboard**: FastAPI + Jinja2 + Alpine.js (CDN, no JS build) on ECS Fargate behind an ALB with Cognito OIDC auth.
- **Auth**: Amazon Cognito (single user pool covers ALB + API Gateway).
- **HITL**: GitHub PR reviews/comments → webhook → EventBridge event → `event_projector` advances DDB state → `state_router` dispatches the next side-effect.
- **IaC**: Terraform only (`hashicorp/aws ~> 6`).

## Key directories

| Path | Role |
|------|------|
| `packages/common/` | Pydantic event envelopes, hybrid-memory utility, OTEL setup, shared boto3 wrappers. |
| `agents/architect/` | Strands agent — writes the three-doc spec bundle (requirements + design + tasks). |
| `agents/critic/` | Strands agent — adversarially reviews the spec (advisory). |
| `agents/implementer/` | Claude Agent SDK agent — opens code PRs. |
| `agents/reviewer/` | Strands agent — code-reviews each task PR (advisory). |
| `agents/tester/` | Strands agent — flags test gaps in each task PR (advisory). |
| `agents/triage/` | Strands agent — classifies issue-driven runs (`proceed` / `ask` / `defer` / `decline`). |
| `agents/proposer/` | Strands agent — schedule/regression-driven; opens PRs proposing prompt or MEMORY.md edits. |
| `lambdas/entry_adapter/` | API Gateway → DDB run row + EventBridge `REQUEST.RECEIVED` + SQS beacon. |
| `lambdas/state_router/` | SQS beacon consumer; reads DDB state and dispatches the next side-effect (agent invoke, repo op, event emit). Never writes state. |
| `lambdas/event_projector/` | EventBridge events → DDB state advance (sole writer of `current_state`) + AgentCore Memory `CreateEvent`. |
| `lambdas/artifact_tool/` | AgentCore Gateway target — S3 + `MEMORY.md` ops. |
| `lambdas/repo_helper/` | AgentCore Gateway target — git/GitHub ops. |
| `lambdas/telemetry/` | Categorises `SPEC.REJECTED` / `TASK.REJECTED` events for downstream learning. |
| `services/dashboard/` | FastAPI submission/tracking UI. |
| `terraform/modules/` | Reusable Terraform modules (one per concern). |
| `terraform/envs/{dev,prod}/` | Environment compositions. |
| `terraform/bootstrap/` | One-time S3 + DDB state backend. |
| `docs/ADRs/` | Architectural Decision Records (written by the Architect agent). |
| `docs/MEMORY.md` | Canonical human-reviewed project memory. |

## Memory model

`MEMORY.md` carries repository-scoped context (conventions, ADR bullets, constraints) and is reviewed in PRs. AgentCore Memory carries cross-session facts (user preferences, learned signals) and session events (≤60 days). Sync is one-way: MEMORY.md → AgentCore Memory on every successful session via `CreateEvent`. The reverse only happens through agent-proposed PR edits — humans gate writes to MEMORY.md.

## Adding a new agent

1. `cp -r agents/architect agents/<name>` and rename module + Dockerfile entrypoint.
2. Add the package as a workspace member (no action needed if it lives under `agents/*`).
3. Implement the agent in `src/<name>/agent.py` and the AgentCore Runtime shell in `src/<name>/app.py`.
4. Add a Terraform `module "agent_<name>" { source = "../../modules/agentcore_runtime" ... }` in the env file.
5. Add the corresponding state(s) to `packages/common/src/common/state.py`, transitions to `packages/common/src/common/state_transitions.py`, and a dispatch handler in `lambdas/state_router/src/state_router/dispatch.py`.

## Running tests

```bash
uv run pytest -q                                     # unit tests only
uv run pytest -m integration                          # moto-backed integration
uv run pytest -m live_aws tests/integration/...       # full end-to-end against dev account (gated)
```

## Deploying

Image build and Terraform apply are GitHub Actions workflows. Production applies require manual approval via GitHub Environments.

```bash
gh workflow run images-build.yml --ref main           # all seven agents → ECR
gh workflow run dashboard-build.yml --ref main         # dashboard container → ECR + ECS update-service
gh workflow run terraform-apply.yml --ref main         # apply (dev auto, prod gated)
```

## Local development

Each agent runs unchanged on a laptop — Strands' `Agent` and Claude Agent SDK's `ClaudeSDKClient` produce the same behaviour locally and on AgentCore Runtime.

```bash
cd agents/architect && uv sync && AIDLC_ENV=dev uv run python -m architect.app
# Hit it on :8080/invocations with the Bedrock AgentCore session header.
```

The dashboard runs locally with `uv run uvicorn dashboard.app:app --reload --port 8080` from `services/dashboard/`. Cognito OIDC is bypassed in dev mode (set `AIDLC_AUTH=disabled`).

## Lint, type, test policy

Inherits the global `~/.claude/CLAUDE.md` standards (≤100 lines/function, complexity ≤8, ≤5 positional params, 100-char lines, absolute imports, Google-style docstrings, zero warnings). Project-specific overrides: none.

## Implementer guardrails (deny-list, enforced by hooks)

- Any `rm -rf /`, `rm -rf $HOME`, `chmod -R 777`, `git push --force-with-lease origin main`.
- Any `aws iam *Delete*`, `terraform apply` against `prod`, `kubectl delete`, `dropdb` / `DROP TABLE`.
- Direct GitHub OAuth tokens or Bedrock model API keys in code.

The implementer container has outbound network access (`Bash` / `WebFetch` / `WebSearch`). Container credentials are scoped (Bedrock + project S3 + GitHub App for the target repo) and the only path code reaches the repo is a human-reviewed PR — that's the load-bearing control, not egress filtering.
