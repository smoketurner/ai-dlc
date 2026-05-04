# ai-dlc — Project manifest

An agentic SDLC platform built on AWS Bedrock AgentCore. The full architectural reference is [`docs/aws-agent-architecture-guide.md`](docs/aws-agent-architecture-guide.md). Read it before making non-trivial changes.

## Tech stack

- **Python 3.14** with the Astral toolchain (`uv` workspace, `ruff`, `ty`).
- **Agents**: Strands Agents (Architect, Critic, Reviewer, Tester) and Claude Agent SDK (Implementer), all shipped as `linux/arm64` containers on Bedrock AgentCore Runtime.
- **Models**: Architect / Critic → Claude Opus 4.7. Implementer / Reviewer → Claude Sonnet 4.6. Tester + memory consolidation → Claude Haiku 4.5.
- **Orchestration**: AWS Step Functions (Standard) using the native `aws-sdk:bedrockagentcore:invokeAgentRuntime` integration.
- **Eventing**: Amazon EventBridge (custom bus + schema registry), DynamoDB streams, SQS DLQs.
- **Memory**: AgentCore Memory (semantic + summarization strategies) plus per-project `MEMORY.md` files in the AgentCore Runtime persistent filesystem (snapshotted to S3).
- **Dashboard**: FastAPI + Jinja2 + Alpine.js (CDN, no JS build) on ECS Fargate behind an ALB with Cognito OIDC auth.
- **Auth**: Amazon Cognito (single user pool covers ALB + API Gateway).
- **HITL**: GitHub PR reviews/comments → webhook → `hitl_handler` Lambda → Step Functions `SendTaskSuccess`.
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
| `agents/proposer/` | Strands agent — schedule/regression-driven; opens PRs proposing prompt or MEMORY.md edits. |
| `lambdas/entry_adapter/` | API Gateway → EventBridge `REQUEST.RECEIVED`. |
| `lambdas/hitl_handler/` | Step Functions `.waitForTaskToken` request + GitHub webhook DECIDE. |
| `lambdas/event_projector/` | DynamoDB Streams + EventBridge → DDB read model + AgentCore Memory `CreateEvent`. |
| `lambdas/artifact_tool/` | AgentCore Gateway target — S3 + `MEMORY.md` ops. |
| `lambdas/repo_helper/` | AgentCore Gateway target — git/GitHub ops. |
| `services/dashboard/` | FastAPI submission/tracking UI. |
| `terraform/modules/` | Reusable Terraform modules (one per concern). |
| `terraform/envs/{dev,prod}/` | Environment compositions. |
| `terraform/bootstrap/` | One-time S3 + DDB state backend. |
| `docs/ADRs/` | Architectural Decision Records (written by the Architect agent). |
| `docs/MEMORY.md` | Canonical human-reviewed project memory. |
| `docs/eval-set/` | Representative SDLC tasks for AgentCore Evaluations. |

## Memory model

`MEMORY.md` carries repository-scoped context (conventions, ADR bullets, constraints) and is reviewed in PRs. AgentCore Memory carries cross-session facts (user preferences, learned signals) and session events (≤60 days). Sync is one-way: MEMORY.md → AgentCore Memory on every successful session via `CreateEvent`. The reverse only happens through agent-proposed PR edits — humans gate writes to MEMORY.md.

## Adding a new agent

1. `cp -r agents/architect agents/<name>` and rename module + Dockerfile entrypoint.
2. Add the package as a workspace member (no action needed if it lives under `agents/*`).
3. Implement the agent in `src/<name>/agent.py` and the AgentCore Runtime shell in `src/<name>/app.py`.
4. Add a Terraform `module "agent_<name>" { source = "../../modules/agentcore_runtime" ... }` in the env file.
5. Add 3+ representative eval cases under `docs/eval-set/<name>/`.
6. Wire the agent into `terraform/modules/sdlc_workflow/asl.tf` as a new task state.

## Running tests

```bash
uv run pytest -q                                     # unit tests only
uv run pytest -m integration                          # moto-backed integration
uv run pytest -m live_aws tests/integration/...       # full end-to-end against dev account (gated)
uv run pytest -m eval                                 # the 10 SDLC eval cases
```

## Deploying

Image build and Terraform apply are GitHub Actions workflows. Production applies require manual approval via GitHub Environments.

```bash
gh workflow run images-build.yml --ref main           # architect + critic + implementer + reviewer + tester → ECR
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
- Network egress outside the AgentCore Gateway.
- Direct GitHub OAuth tokens or Bedrock model API keys in code.
