# Roadmap

Live tracker for the AI-DLC initial build. The full architectural plan lives at [`aws-agent-architecture-guide.md`](aws-agent-architecture-guide.md); a frozen execution plan from the planning session is preserved at `~/.claude/plans/i-want-to-start-shimmering-dewdrop.md`.

Each phase below has a checklist. As work lands, check the box. New work that comes out of execution but doesn't belong to the current phase goes to **Parking lot** at the bottom — and gets a corresponding GitHub issue (label `parking-lot`).

Legend: ✅ done · 🟡 in progress · ⬜ todo

---

## Phase 0 — Repo scaffolding ✅

- [x] `pyproject.toml` (workspace root, ruff strict, ty strict, pytest config)
- [x] `.python-version` = `3.14`
- [x] `.gitignore` (Python + Terraform)
- [x] `.pre-commit-config.yaml` (prek-compatible, all third-party hooks SHA-pinned)
- [x] `.github/workflows/ci.yml` (ruff + ty + pytest + pip-audit + zizmor; all actions SHA-pinned)
- [x] `README.md`
- [x] `CLAUDE.md` (project manifest)
- [x] `docs/MEMORY.md` (template — six sections)
- [x] `docs/ROADMAP.md` (this file)
- [x] Empty directory tree for `packages/`, `agents/`, `lambdas/`, `services/`, `terraform/`, `tests/`
- [x] `uv sync && uv run ruff check && uv run ruff format --check && uv run ty check` all green

## Phase 1 — `packages/common` 🟡

Shared package every other component depends on. Lambdas pull from `common`; the dashboard and agents do too.

- [x] `packages/common/pyproject.toml` (workspace member; pydantic 2.13.3, boto3 1.43.2, structlog 25.5.0, OTEL 1.40.0, mcp 1.27.0, bedrock-agentcore 1.8.0, etc., all exact-pinned)
- [x] `src/common/events.py` — `EventEnvelope[T]` (PEP 695 generic) + 9 typed payload models, frozen + strict + `extra="forbid"`
- [x] `src/common/ids.py` — UUID7 helpers via `uuid-utils`
- [x] `src/common/errors.py` — `AidlcError` base + 11 typed subclasses with structured context
- [x] `src/common/settings.py` — `pydantic-settings` Settings, frozen, `AIDLC_*` env prefix
- [x] `src/common/telemetry.py` — structlog JSON config + OTEL `agent_span` / `tool_span` / `record_tokens`
- [x] `src/common/s3.py` — typed wrappers around the `mypy_boto3_s3.client.S3Client` (put_text/get_text/list_keys with KMS-SSE)
- [x] `src/common/agentcore_memory.py` — typed wrappers around `BedrockAgentCoreClient` (`create_event`, `retrieve_memory_records`)
- [x] `src/common/memory_md.py` — strict 6-section parser/renderer; fail-fast on unknown headers or out-of-order sections
- [x] `src/common/memory.py` — hybrid memory orchestrator (load_memory_md / save_memory_md / sync_to_agentcore / retrieve_relevant_memory)
- [x] `src/common/gateway.py` — minimal MCP JSON-RPC client to AgentCore Gateway
- [x] `src/common/git_ops.py` — `subprocess`-based git helpers for the Implementer's persistent FS
- [x] `src/common/runtime.py` — `InvocationPayload` model used by every agent's `/invocations` entrypoint
- [x] `tests/` — 26 tests pass (errors, ids, events, memory_md, settings); `ruff check`, `ruff format --check`, `ty check` all green
- [ ] tests for `s3`, `agentcore_memory`, `memory`, `gateway`, `git_ops`, `telemetry` (deferred — written alongside their first integration in Phases 3–6, with `moto` for AWS and a real `BedrockAgentCoreClient` stub via `pytest-mock`)

## Phase 2 — Terraform foundation ⬜

Infrastructure that everything else lives on. Single PR, single apply.

- [ ] `terraform/bootstrap/` — S3 tfstate bucket + DDB lock table (one-time)
- [ ] `terraform/envs/dev/{backend.tf, providers.tf, main.tf, variables.tf, outputs.tf, locals.tf, terraform.tfvars}`
- [ ] `terraform/modules/network/` — VPC, subnets, SGs, VPC endpoints
- [ ] `terraform/modules/kms/` — six CMKs with rotation
- [ ] `terraform/modules/iam/` — baseline roles + Cedar stub (gated off)
- [ ] `terraform/modules/s3_artifacts/` — artifacts + memory_md buckets
- [ ] `terraform/modules/dynamodb_state/` — runs / idempotency_keys / approvals
- [ ] `terraform/modules/ecr_agents/` — architect + implementer ECR repos
- [ ] `terraform/modules/cognito/` — user pool + app client + scopes
- [ ] `terraform/modules/eventbridge_bus/` — bus + archive + schema registry + DLQ
- [ ] `terraform/modules/sqs_plumbing/` — HITL queue + DLQs
- [ ] `terraform/modules/github_oidc/` — GH Actions assume-role
- [ ] `terraform/modules/observability/` — log groups, metric filters, alarms baseline, SNS, dashboard
- [ ] `terraform-plan.yml` + `terraform-apply.yml` workflows
- [ ] `terraform plan && terraform apply` succeeds end-to-end in dev

## Phase 3 — AgentCore identity + memory + gateway ⬜

- [ ] `terraform/modules/agentcore_identity/` — workload_identity ×2 + oauth2_credential_provider (GitHub) + token_vault_cmk
- [ ] `terraform/modules/agentcore_memory/` — memory + 3 strategies (semantic_project, semantic_user, summarization_session)
- [ ] `lambdas/artifact_tool/` — gateway target Lambda (S3 + MEMORY.md ops)
- [ ] `lambdas/repo_helper/` — gateway target Lambda (git/GitHub ops)
- [ ] `terraform/modules/agentcore_gateway/` — gateway + 3 targets (GitHub MCP, artifact_tool, repo_helper)
- [ ] Manual MCP `list_tools` against the gateway returns expected tool catalog

## Phase 4 — Architect agent ⬜

- [ ] `agents/architect/pyproject.toml`
- [ ] `agents/architect/Dockerfile` (python:3.14-slim, ARM64, multi-stage uv)
- [ ] `agents/architect/src/architect/{app.py, agent.py, prompts.py, tools.py, adr.py}`
- [ ] `agents/architect/tests/`
- [ ] `images-build.yml` workflow (docker buildx ARM64 → ECR by SHA; cosign-sign)
- [ ] `terraform/modules/agentcore_runtime/` parameterized module
- [ ] `module "agent_architect"` in `envs/dev/main.tf`
- [ ] Local smoke: `uv run python -m architect.app` against dev memory + gateway
- [ ] AWS smoke: `aws bedrock-agentcore-runtime invoke-agent-runtime ...` returns ADR S3 key

## Phase 5 — Lambdas + Step Functions + API Gateway ⬜

- [ ] `lambdas/entry_adapter/` — API GW → events:PutEvents REQUEST.RECEIVED
- [ ] `lambdas/hitl_handler/` — `.waitForTaskToken` REQUEST_APPROVAL + DECIDE (called by dashboard or API GW)
- [ ] `lambdas/event_projector/` — DDB Streams + EventBridge → DDB read model + AgentCore Memory `CreateEvent`
- [ ] `lambdas-build.yml` workflow (uv build → zip → S3)
- [ ] `terraform/modules/lambdas/{entry_adapter, hitl_handler, event_projector}/`
- [ ] `terraform/modules/sdlc_workflow/` — Step Functions Standard with native `aws-sdk:bedrockagentcore:invokeAgentRuntime` ASL
- [ ] `terraform/modules/api_gateway/` — HTTP API + JWT authorizer + routes
- [ ] End-to-end smoke: `POST /v1/runs` → run reaches `WaitForArchApproval` → approve via API → `RUN.COMPLETED` (with stub Implementer)

## Phase 6 — Implementer agent ⬜

- [ ] `agents/implementer/pyproject.toml`
- [ ] `agents/implementer/Dockerfile`
- [ ] `agents/implementer/src/implementer/{app.py, client.py, options.py, tools.py, hooks.py, prompts.py}`
- [ ] `agents/implementer/src/implementer/skills/{ai-dlc-conventions, memory-md-writer}/`
- [ ] `module "agent_implementer"` in `envs/dev/main.tf`
- [ ] Wire `InvokeImplementer` task state into `sdlc_workflow` ASL
- [ ] Full pipeline: `POST /v1/runs` → ADR PR → approve → code PR → approve → `RUN.COMPLETED`

## Phase 7 — Dashboard ⬜

- [ ] `services/dashboard/pyproject.toml` (FastAPI, Jinja2, sse-starlette, httpx)
- [ ] `services/dashboard/Dockerfile`
- [ ] `services/dashboard/src/dashboard/{app.py, auth.py, deps.py, repos.py, models.py}`
- [ ] `services/dashboard/src/dashboard/routes/{pages.py, runs.py, stream.py, webhooks.py}`
- [ ] Templates: `base.html`, `runs.html`, `run_detail.html`, `approvals.html`, `submit.html`
- [ ] `terraform/modules/{ecr_dashboard, ecs_dashboard, alb_dashboard}/`
- [ ] `dashboard-build.yml` workflow
- [ ] GitHub PR webhook integration via ALB listener rule (HMAC-verified)
- [ ] Smoke test: submit run from UI → live SSE updates → approve via PR comment → state changes within ~2 s

## Phase 8 — Eval set + observability hardening ⬜

- [ ] `docs/eval-set/` with 10 representative SDLC tasks
- [ ] AgentCore Evaluations wiring (when GA)
- [ ] Recommendations + Batch Evaluations + A/B Tests loop
- [ ] Tighten alarm thresholds based on observed dev traffic
- [ ] Dashboard: cost-per-run + token-usage breakdown panels

---

## Parking lot

Items that came out of execution and aren't on the critical path. Each one is a GitHub issue tagged `parking-lot`. Filter at https://github.com/smoketurner/ai-dlc/issues?q=is%3Aopen+label%3Aparking-lot.

- [ ] [#1 — Switch AgentCore Runtime to VPC mode](https://github.com/smoketurner/ai-dlc/issues/1)
- [ ] [#3 — Migrate AgentCore Runtime to AgentCore Harness when GA](https://github.com/smoketurner/ai-dlc/issues/3)
- [ ] [#4 — Support agent sessions longer than 1 hour via `.waitForTaskToken`](https://github.com/smoketurner/ai-dlc/issues/4)
- [ ] [#5 — Enable A2A protocol for cross-team or third-agent invocation](https://github.com/smoketurner/ai-dlc/issues/5)
- [ ] [#6 — Measure and document MEMORY.md → AgentCore Memory async lag](https://github.com/smoketurner/ai-dlc/issues/6)
- [ ] [#9 — Enforce per-run cost hard cap](https://github.com/smoketurner/ai-dlc/issues/9)
- [ ] [#10 — Wire custom domain for the dashboard](https://github.com/smoketurner/ai-dlc/issues/10)
- [ ] [#11 — Tune persistent FS retention based on real paused-session data](https://github.com/smoketurner/ai-dlc/issues/11)
- [ ] [#12 — Add Slack-based HITL approvals for non-engineer reviewers](https://github.com/smoketurner/ai-dlc/issues/12)
- [ ] [#13 — Add AgentCore Browser + Code Interpreter when an agent needs them](https://github.com/smoketurner/ai-dlc/issues/13)
- [ ] [#14 — Add actionlint to CI alongside zizmor](https://github.com/smoketurner/ai-dlc/issues/14)
- [ ] [#15 — Add Playwright E2E tests for the dashboard](https://github.com/smoketurner/ai-dlc/issues/15)

### Decided not to do

These were considered, then explicitly declined. Don't re-propose without a concrete trigger that wasn't true at the time of the decision.

- ~~[#2 — Enable Cedar / Verified Permissions for cross-agent RBAC](https://github.com/smoketurner/ai-dlc/issues/2)~~ — closed 2026-05-01. Per-agent IAM roles + resource-tag conditions on AgentCore Memory and Gateway targets are sufficient.
- ~~[#7 — Add Langfuse or Datadog as OTEL trace backend](https://github.com/smoketurner/ai-dlc/issues/7)~~ — closed 2026-05-01. CloudWatch (with OTEL auto-export from AgentCore Runtime) is the trace backend; relay can be added later as a code change without rearchitecting.
- ~~[#8 — Migrate to multi-account AWS Org / Control Tower](https://github.com/smoketurner/ai-dlc/issues/8)~~ — closed 2026-05-01. Single AWS account with env separation is the long-term plan.

---

## How to use this file

1. When you finish a checkbox, mark it `[x]` in the same PR that contains the change.
2. When you discover work that's not in the current phase, drop it in **Parking lot** and link to the corresponding GitHub issue (once it exists).
3. When a phase completes, update its header from 🟡 to ✅.
4. Don't gold-plate phases. Promote items from Parking lot only when there's a concrete trigger.
