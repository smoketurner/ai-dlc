# Roadmap

Live tracker for the AI-DLC initial build. The full architectural plan lives at [`aws-agent-architecture-guide.md`](aws-agent-architecture-guide.md); a frozen execution plan from the planning session is preserved at `~/.claude/plans/i-want-to-start-shimmering-dewdrop.md`.

Each phase below has a checklist. As work lands, check the box. New work that comes out of execution but doesn't belong to the current phase goes to **Parking lot** at the bottom — and gets a corresponding GitHub issue (label `parking-lot`).

Legend: ✅ done · 🟡 in progress · ⬜ todo

---

## Pipeline shape (spec-driven)

The platform follows a spec-driven SDLC inspired by Kiro's three-document model:

```
REQUEST.RECEIVED
  → SPEC.READY     (Architect writes requirements + design + tasks)
  → SPEC.APPROVED  (gate 1 — reviewer signs off on the whole spec bundle)
  → TASK.READY     ┐
  → TASK.APPROVED  │ loop while tasks remain — one PR per task
  → ...            ┘
  → RUN.COMPLETED
```

- **Specs** live at `docs/specs/{slug}/{requirements,design,tasks}.md` (template in [`docs/specs/_template/`](specs/_template/)).
- **ADRs** at `docs/ADRs/NNNN-slug.md` capture cross-cutting architectural decisions; most specs don't produce one.
- **Agents**: Architect (Strands, Opus 4.7) and Implementer (Claude Agent SDK, Sonnet 4.6).
- **Events** (9 types): `REQUEST.RECEIVED`, `SPEC.{READY,APPROVED,REJECTED}`, `TASK.{READY,APPROVED,REJECTED}`, `RUN.{COMPLETED,FAILED}`.

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

## Phase 2 — Terraform foundation 🟡

Infrastructure that everything else lives on. Single PR, single apply.

- [x] `terraform/bootstrap/` — S3 tfstate bucket (uses S3 native lockfile; no DDB lock table needed)
- [x] `terraform/envs/dev/{backend.tf, providers.tf, main.tf, variables.tf, outputs.tf, terraform.tfvars}`
- [x] `terraform/modules/network/` — VPC, subnets, SGs, VPC endpoints (delegates to terraform-aws-modules/vpc)
- [x] `terraform/modules/crypto/` — six CMKs with rotation (renamed from `kms/`)
- [x] `terraform/modules/state/` — artifacts + memory_md buckets and runs / idempotency_keys / approvals tables (combines `s3_artifacts` + `dynamodb_state`)
- [x] `terraform/modules/registry/` — architect + implementer + dashboard ECR repos (renamed from `ecr_agents/`)
- [x] `terraform/modules/auth/` — Cognito user pool + app client + scopes (renamed from `cognito/`)
- [x] `terraform/modules/messaging/` — bus + archive + schema registry + HITL/EB DLQs (combines `eventbridge_bus` + `sqs_plumbing`)
- [x] `terraform/modules/ci_cd/` — GitHub Actions OIDC provider + terraform / image_publisher roles (renamed from `github_oidc/`)
- [x] `terraform/modules/observability/` — log groups, alarms baseline, SNS, dashboard
- [ ] `terraform plan && terraform apply` succeeds end-to-end in dev (run locally — see `terraform/Makefile`)

**Design notes:**
- The standalone `iam/` module from the original plan was folded into per-consumer module IAM (each Lambda module owns its execution role; `ci_cd` owns CI roles). No shared baseline module is needed; this avoids cross-module coupling on role names. Cedar / Verified Permissions was decided-against in the parking lot.
- Terraform `plan` / `apply` runs **locally** via `make -C terraform plan` / `make -C terraform apply` — no GitHub Actions workflow. The `ci_cd` module still publishes the OIDC provider + `image_publisher` role for the image-build workflows in later phases; the `terraform` role it provisions is reserved for any future shift back to CI-driven applies.

## Phase 3 — Agent substrate (memory + identity + per-agent gateways) 🟡

Consolidated into a single `agents` Terraform module since identity, memory, gateway, and tool surface are one logical concern. Per AWS guidance, each agent gets its own gateway (separate IAM/JWT scope, smaller blast radius); both agents share the memory store and the tool Lambdas.

- [x] `terraform/modules/agents/` — workload_identity per agent, GitHub oauth2 credential provider (gated), token_vault_cmk on `tokenvault` KMS key, AgentCore Memory + 4 strategies (`SEMANTIC` / `USER_PREFERENCE` / `SUMMARIZATION` / `EPISODIC`), per-agent AgentCore Gateway with Cognito JWT auth, and `(agent × tool)` gateway targets via `for_each`.
- [x] `lambdas/artifact_tool/` — S3 + MEMORY.md operations (`put_artifact`, `get_artifact`, `list_artifacts`, `read_memory_md`, `write_memory_md`); 6 unit tests pass under moto.
- [x] `lambdas/repo_helper/` — git / GitHub operations (`open_pr`, `comment_pr`, `create_branch`, `commit_files`, `get_pr`); Phase 3 ships the validated input schemas + stub responses, network calls land in Phase 6.
- [x] Tool Lambdas wired as gateway targets; per-agent gateway role limits `lambda:InvokeFunction` to the subset the agent's `targets` list permits.
- [x] `terraform validate` clean for the dev composition (`module.agents` wired, outputs surfaced).
- [ ] Manual MCP `list_tools` against the live gateway returns expected tool catalog (deferred — requires `terraform apply` against AWS)

**Memory model:** Hybrid. AgentCore Memory holds cross-session learned facts (4 strategies — `SEMANTIC` for project facts, `USER_PREFERENCE` for per-user prefs, `SUMMARIZATION` for session summaries, `EPISODIC` for the rejection-retry loop); the S3 `memory_md` bucket holds canonical per-project `MEMORY.md` and session snapshots. The artifact_tool Lambda reads/writes the S3 side; agents talk to AgentCore Memory directly via the Bedrock SDK. `MEMORY.md` → AgentCore Memory sync runs on every successful session via `CreateEvent`; the reverse path goes through agent-proposed PRs to `docs/MEMORY.md` (humans gate writes).

## Phase 4 — Architect agent 🟡

The Architect produces a three-document spec bundle (`requirements.md`, `design.md`, `tasks.md`) under `docs/specs/{slug}/` and may propose ADRs in the design when a cross-cutting decision surfaces.

- [x] `agents/architect/pyproject.toml` (workspace member; strands-agents 1.38, bedrock-agentcore 1.8, common path-dep)
- [x] `agents/architect/Dockerfile` (python:3.14-slim, ARM64, multi-stage uv)
- [x] `agents/architect/src/architect/{app.py, agent.py, prompts.py, tools.py, spec.py}` — `spec.py` owns the three-doc Pydantic models + Markdown renderer; `tools.py` exposes plain functions + Strands `@tool` wrappers; `agent.py` uses `Agent.structured_output(SpecBundle, …)` against Opus 4.7
- [x] `agents/architect/tests/test_spec.py` — 10 unit tests on validation + Markdown rendering
- [x] `images-build.yml` workflow (docker buildx ARM64 → ECR by SHA + `latest`; OIDC-authenticated; matrix over agents)
- [x] AgentCore Runtime resource added to the `agents` Terraform module — per-agent role + ECR-digest-pinned container + Cognito JWT authorizer; gated on `image_tag != ""` so initial apply runs without a pushed image
- [x] `module.agents` in `envs/dev/main.tf` consumes ECR repo URLs and per-agent image tags
- [ ] Local smoke: `uv run python -m architect.app` against dev memory + gateway (deferred — needs Bedrock model access)
- [ ] AWS smoke: build + push image, set `architect_image_tag = "<sha>"`, apply, then `aws bedrock-agentcore-runtime invoke-agent-runtime ...` returns `spec_s3_prefix`

## Phase 5 — Pipeline orchestration 🟡

Consolidated into a single `pipeline` Terraform module per the logical-groupings preference: 3 platform Lambdas + the Step Functions state machine + API Gateway live in one module.

- [x] `lambdas/entry_adapter/` — POST /v1/runs body → idempotency-keyed DDB put → events:PutEvents `REQUEST.RECEIVED`; powertools Logger; 5 unit tests under moto.
- [x] `lambdas/hitl_handler/` — Two ops: `REQUEST_APPROVAL` (Step Functions `.waitForTaskToken` caller, persists token in approvals table) and `DECIDE` (resolves a gate by calling SendTaskSuccess/Failure); 5 unit tests.
- [x] `lambdas/event_projector/` — EventBridge consumer (single-event payload) + DDB Streams batch consumer (passthrough placeholder); writes the runs read-model row + forwards envelope to AgentCore Memory `CreateEvent`; 5 unit tests with mocked AgentCore client.
- [x] `terraform/modules/pipeline/` — single module: 3 Lambdas via `terraform-aws-modules/lambda` (build_in_docker arm64), Step Functions Standard state machine using JSONata + `aws-sdk:bedrockagentcore:invokeAgentRuntime` native integration, HTTP API Gateway with Cognito JWT authorizer, EventBridge → projector wiring.
- [x] State machine ASL (`asl/sdlc.asl.json.tftpl`): `Receive → PutInitialState → InvokeArchitect → PublishSpecReady → WaitForSpecApproval → IterateTasks (Map MaxConcurrency=1) { InvokeImplementer → PublishTaskReady → WaitForTaskApproval } → PublishCompleted`. `MarkFailed` catches every failure path and emits `RUN.FAILED`.
- [x] API Gateway routes: `POST /v1/runs` (JWT, → entry_adapter), `POST /v1/runs/{run_id}/decide` (JWT, → hitl_handler DECIDE), `POST /webhooks/github` (no auth, reserved for the dashboard's HMAC-verified handler in Phase 7).
- [x] `module.pipeline` wired into `envs/dev/main.tf`; outputs the API endpoint, state-machine ARN, and platform Lambda ARNs.
- [ ] Lambda zip build deferred from CI — terraform-aws-modules/lambda packages on apply with `build_in_docker = true`. No standalone `lambdas-build.yml` workflow needed; revisit if local Docker becomes a bottleneck.
- [ ] End-to-end smoke (deferred — needs live AWS): `POST /v1/runs` → run reaches `WaitForSpecApproval` → approve via API → first TASK gate → approve → `RUN.COMPLETED`.

## Phase 6 — Implementer agent 🟡

The Implementer reads an approved spec, picks one unchecked task from `tasks.md` by id, and opens **one PR for that task only**. On approval, the SDLC pipeline's Map state advances to the next task; the loop terminates when every task has been approved.

- [x] `agents/implementer/pyproject.toml` (workspace member; claude-agent-sdk 0.1.72, bedrock-agentcore 1.8, common path-dep, httpx, pydantic)
- [x] `agents/implementer/Dockerfile` (python:3.14-slim ARM64; Node 22 + `@anthropic-ai/claude-code` for the SDK CLI subprocess; git for repo ops)
- [x] `agents/implementer/src/implementer/{app.py, client.py, options.py, hooks.py, prompts.py, tasks.py, repo_ops.py}` — `tasks.py` parses + flips checkboxes (10 unit tests); `hooks.py` enforces the deny-list at the PreToolUse boundary; `repo_ops.py` wraps git + GitHub REST.
- [x] Step Functions Map state already iterates per-task; the Implementer's `ImplementerInput`/`ImplementerResult` contract matches the existing `InvokeImplementer` ASL.
- [x] `module.agents` runtime resource is generic over agents — flip `implementer_image_tag` once CI pushes an image and the runtime is provisioned.
- [ ] Skills (`ai-dlc-conventions`, `memory-md-writer`) — deferred. Phase 6 ships without Claude Skills; system prompt + hooks cover guard-rails. Promote when an actual gap appears.
- [ ] Full pipeline smoke (deferred — needs live AWS + GitHub OAuth): `POST /v1/runs` → spec PR → approve → task-1 PR → approve → ... → `RUN.COMPLETED`.

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
