# Roadmap

Live tracker for the AI-DLC initial build. The full architectural plan lives at [`aws-agent-architecture-guide.md`](aws-agent-architecture-guide.md); a frozen execution plan from the planning session is preserved at `~/.claude/plans/i-want-to-start-shimmering-dewdrop.md`.

Each phase below has a checklist. As work lands, check the box. New work that comes out of execution but doesn't belong to the current phase goes to **Parking lot** at the bottom — and gets a corresponding GitHub issue (label `parking-lot`).

Legend: ✅ done · 🟡 in progress · ⬜ todo

---

## Pipeline shape (spec-driven)

The platform follows a spec-driven SDLC inspired by Kiro's three-document model:

```
REQUEST.RECEIVED
  → SPEC.READY        (Architect writes requirements + design + tasks)
  → CRITIQUE.READY    (Critic adversarially reviews the spec — advisory)
  → SPEC.APPROVED     (gate 1 — human reviewer)
  → TASK.READY        ┐
  → REVIEW.READY      │ Reviewer code-reviews the PR — advisory
  → TEST_REPORT.READY │ Tester flags test gaps — advisory
  → TASK.APPROVED     │ loop while tasks remain — one PR per task
  → ...               ┘
  → RUN.COMPLETED
```

- **Specs** live at `docs/specs/{slug}/{requirements,design,tasks}.md` (template in [`docs/specs/_template/`](specs/_template/)).
- **ADRs** at `docs/ADRs/NNNN-slug.md` capture cross-cutting architectural decisions; most specs don't produce one.
- **Agents** (6): Architect / Critic / Reviewer / Tester / Proposer (Strands) and Implementer (Claude Agent SDK). Architect + Critic + Proposer use Opus 4.7; Implementer + Reviewer use Sonnet 4.6; Tester + memory consolidation use Haiku 4.5.
- **Events** (11 types): `REQUEST.RECEIVED`, `SPEC.{READY,APPROVED,REJECTED}`, `CRITIQUE.READY`, `TASK.{READY,APPROVED,REJECTED}`, `REVIEW.READY`, `TEST_REPORT.READY`, `RUN.{COMPLETED,FAILED}`.

> **Phase 12 extends this with an autonomous, GitHub-issue-driven entry path.** A new Triage agent inspects an issue assigned to the bot and decides whether to *proceed* (routing into one of four workflow phases — `spec_driven`, `bug_fix`, `upgrade`, `docs`), *ask* for clarification by commenting on the issue, *defer*, or *decline*. The diagram above describes the `spec_driven` phase; the others land in 12b. The HITL gates collapse: per-task PR approval moves from Step Functions to GitHub itself — TWO-WAY PRs merge on green review, ONE-WAY PRs open as draft and require a human to mark them ready. See Phase 12.

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
- [x] `tests/` — 37 tests pass (errors, ids, events, memory_md, routing, settings); `ruff check`, `ruff format --check`, `ty check` all green
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
- [x] `lambdas/repo_helper/` — replaced Phase 3 stubs with real GitHub REST calls (open_pr, comment_pr, create_branch, commit_files via Git Data API, get_pr). Two auth paths: **user on-behalf-of** via AgentCore Identity `GithubOauth2` credential provider (`USER_FEDERATION` flow → Token Vault → `GetWorkloadAccessTokenForUserId` + `GetResourceOauth2Token`) and **installation-token fallback** minted from the App's private key in Secrets Manager. 14 unit tests covering both paths via `httpx.MockTransport`.
- [x] Terraform `agents` module: `github_oauth` → `github_app` (adds `app_id` + `private_key`); kept the `GithubOauth2` credential provider for user OBO; added Secrets Manager secret for App credentials; added repo_helper workload identity; wired env vars + IAM (Secrets Manager + `bedrock-agentcore:GetWorkloadAccessTokenForUserId` + `bedrock-agentcore:GetResourceOauth2Token`) to the repo_helper Lambda.
- [x] Dashboard "Connect GitHub" flow + threading `requestor_sub` (Cognito sub) into agent inputs and through to the repo_helper tool calls — **shipped in Phase 11** (see below). Commits attribute to the requestor when they've linked GitHub; fall back to `ai-dlc[bot]` otherwise.
- [ ] Skills (`ai-dlc-conventions`, `memory-md-writer`) — deferred. Phase 6 ships without Claude Skills; system prompt + hooks cover guard-rails. Promote when an actual gap appears.
- [ ] Full pipeline smoke (deferred — needs live AWS + a real GitHub App install): `POST /v1/runs` → spec PR → approve → task-1 PR → approve → ... → `RUN.COMPLETED`.

## Phase 7 — Dashboard 🟡

Per the logical-groupings preference, the three planned modules (`ecr_dashboard`, `ecs_dashboard`, `alb_dashboard`) collapsed into a single `terraform/modules/dashboard/` module that owns the ECS cluster + service + ALB + listener rules. The dashboard ECR repo lives in the existing `registry` module alongside the agent repos.

- [x] `services/dashboard/pyproject.toml` — FastAPI 0.136, Jinja2, sse-starlette, httpx, pyjwt[crypto] (ALB OIDC verification), uvicorn[standard].
- [x] `services/dashboard/Dockerfile` — python:3.14-slim ARM64, multi-stage uv build, runs uvicorn on :8080.
- [x] `services/dashboard/src/dashboard/{app, auth, deps, repos, models}.py` — boto3 clients cached at process scope; ALB-injected `x-amzn-oidc-data` decoded via the per-region public key endpoint; `AIDLC_AUTH=disabled` short-circuits auth in dev.
- [x] `services/dashboard/src/dashboard/routes/{pages, runs, stream, webhooks}.py` — server-rendered HTML pages, JSON `POST /v1/runs` (idempotency-keyed → events:PutEvents), SSE `GET /v1/runs/{id}/stream` polling DDB every 1 s, HMAC-verified `POST /webhooks/github` → `hitl_handler` Lambda invoke.
- [x] Jinja2 templates: `base.html` (Tailwind + Alpine.js via CDN), `runs.html`, `run_detail.html` (Alpine `EventSource` subscriber), `approvals.html` (deep-links to GitHub PRs), `submit.html`.
- [x] `terraform/modules/dashboard/` — single module: ECS Fargate cluster (Container Insights on, ARM64), task definition (digest-pinned image, 0.5 vCPU / 1 GB), service in private subnets + autoscaling 1-4 / CPU 60 %, ALB in public subnets with HTTPS listener (Cognito OIDC `authenticate-cognito` action) and a separate `/webhooks/github` listener rule that bypasses auth. Service is gated on `image_tag != ""`.
- [x] `dashboard-build.yml` workflow — lint+type+test → buildx ARM64 → ECR push by SHA + `latest`. zizmor-clean.
- [x] GitHub PR webhook integration via ALB listener rule (HMAC-verified by the dashboard FastAPI route, then forwarded to `hitl_handler.DECIDE`).
- [x] API Gateway `POST /webhooks/github` route removed from the `pipeline` module — the dashboard ALB owns it now (replace, don't deprecate).
- [ ] Smoke test (deferred — needs live AWS): submit run from UI → live SSE updates → approve via PR comment → state changes within ~2 s.

13 unit tests on the webhook handler cover HMAC verify, review-event parsing, comment-magic-string parsing, JSON round-tripping.

## Phase 8 — Eval set + observability hardening 🟡

- [x] `docs/eval-set/` — 10 representative SDLC cases ([README](eval-set/README.md)): empty-repo bootstrap, small feature add, cross-cutting feature, bug fix, refactor, dep upgrade, MEMORY.md learning, conflict resolution, HITL reject loop, long-session resume. Each case has an intent prompt, expected behaviour, and observable pass criteria (cost cap, PR count, files touched, presence of acceptance-criteria coverage). They drive manual smoke testing today and become the AgentCore Evaluations suite when that lands.
- [x] Dashboard cost + token breakdown — `event_projector` upserts a per-run `STATE` row with `tasks_completed` + `total_token_in/out` + `total_cost_usd` + `total_duration_ms` from `RUN.COMPLETED` envelopes; runs list shows tokens-in/out + cost columns; run-detail page shows the full breakdown panel. Two new projector tests cover the state-row upsert + RUN.COMPLETED capture.
- [ ] AgentCore Evaluations wiring (deferred — service is preview, no Terraform support yet). Trigger to revisit: `aws_bedrockagentcore_evaluation` lands in the AWS provider.
- [ ] Recommendations + Batch Evaluations + A/B Tests loop (deferred — same trigger).
- [ ] Tighten alarm thresholds (deferred — needs real dev traffic to set the right p99 / error-rate cutoffs). Trigger: 7+ days of run data on the daily-spend + per-agent metrics.

---

## Phase 9 — Continuous improvement (self-tuning loop) 🟡

The platform should learn from itself. Every rejection is a labeled signal; every clean run is a few-shot example; every model/prompt change is a regression risk. Phase 9 closes the loop, but **never auto-mutates agent prompts** — improvements always land as PRs that humans review (the same trust boundary the platform itself uses).

```
                                       ┌──────────────────┐
   every event ─► EventBridge ─►─►─►─► │  Telemetry agent │  auto-categorize rejection reasons
                                       │  (Haiku 4.5)     │  → labeled records to S3 evals lake
                                       └────────┬─────────┘
                                                │
                                                ▼
                                       ┌──────────────────┐
   approved runs ─► DDB stream ─►─►─►─ │  Few-shot miner  │  mine (intent → spec) + (task → diff)
                                       │                  │  → curated few-shot bank in S3
                                       └────────┬─────────┘
                                                │
   ★ on prompt/model/dep change ─►─►─►─►─►─►─►─►─┤
   ★ on schedule (nightly)      ─►─►─►─►─►─►─►─►─┤
                                                ▼
                                       ┌──────────────────┐
                                       │  Eval runner     │  runs all docs/eval-set/ cases
                                       │  (SF distributed │  through the live pipeline
                                       │   Map)           │  → pass/fail/cost matrix
                                       └────────┬─────────┘
                                                │ regression detected?
                                                ▼
                                       ┌──────────────────┐
                                       │  Improvement     │  ★ runs weekly
                                       │  Proposer        │  reads telemetry + few-shot bank +
                                       │  (Strands/Opus)  │  eval deltas; opens a PR with
                                       │                  │  proposed prompt / MEMORY.md edits
                                       └────────┬─────────┘
                                                │
                                                ▼
                                       PR ─► human reviews ─► merge
                                       (the gate — never auto-apply)
```

Three landings, each independently shippable:

### 9a — Telemetry + Few-shot miner ✅

- [x] `lambdas/telemetry/` — EventBridge-triggered Lambda. Categorizes every `*.REJECTED` reason via Bedrock Haiku 4.5 against the fixed 10-category taxonomy. Writes labeled record to `s3://artifacts/evals/rejections/{date}/{run_id}/{gate_ref}.json` and increments per-run (`category_*`, `total_rejections`) + per-project rolling counters (`PROJECT#{slug}` / `REJECTIONS#{YYYY-MM}`) on the runs table. Falls back to `other` on bad model output — never gates the pipeline. **11 unit tests.**
- [x] `lambdas/few_shot_miner/` — DDB-stream consumer for the runs table with a server-side filter pattern that only forwards `STATE`/`RUN.COMPLETED` rows. On match with `total_rejections == 0`, queries the run's event timeline and writes one `intent_to_spec` example + one `task_to_diff` example per approved task to `s3://artifacts/evals/few-shots/{kind}/{date}/{run_id}/{ix}.json`. **8 unit tests.**
- [x] `terraform/modules/improvement/` — single logical-grouping module owning the self-improvement infrastructure: telemetry + few_shot_miner Lambdas (built via `terraform-aws-modules/lambda` arm64), EventBridge rule routing `SPEC.REJECTED`/`TASK.REJECTED` to telemetry, DDB stream event-source mapping with the filter pattern. Reserved space for 9b/9c.
- [x] `module.improvement` wired into `envs/dev/main.tf`; outputs telemetry + miner Lambda ARNs.

### 9b — Eval runner + Drift detector ✅

- [x] `terraform/modules/improvement/` Step Functions distributed Map state machine maps over `docs/eval-set/cases.yaml` and invokes the live SDLC pipeline once per case via `states:startExecution.sync:2`. Captures pass/fail/cost matrix.
- [x] `lambdas/eval_runner/` — 4 ops: `load_cases` (with `tier_filter`), `evaluate_result`, `record_result` (S3 + per-case CW metric), `aggregate_results` (suite-wide `PassRate`).
- [x] `tier: smoke|full` field on each case in `cases.yaml`. PR-triggered runs use the smoke tier (3 cases: `02-small-feature-add`, `04-bug-fix`, `07-memory-md-learning`); nightly + manual runs use the full suite.
- [x] **HITL auto-approval in eval mode**: SDLC ASL threads `eval_mode` through both gate payloads; `hitl_handler` short-circuits with `SendTaskSuccess` when set. Without this, eval runs would hang at `WaitForSpecApproval`.
- [x] EventBridge schedule (nightly) + `evals.yml` GitHub Actions workflow on PRs touching prompts / model IDs / `docs/MEMORY.md`.
- [x] `lambdas/drift_detector/` — Lambda that compares trailing-7d vs trailing-30d pass rate from S3 records. Persists a structured drift report at `evals/drift/{ts}.json`, emits the `RegressionDetected` CW metric, and publishes a structured alert to the alerts SNS topic when a regression fires. Triggered on each eval-state-machine `ExecutionSucceeded` and on a daily floor schedule.
- [x] CloudWatch alarm `${prefix}-eval-regression` watches `RegressionDetected`; routes to alerts SNS.
- [ ] **PR commenting on the offending change** (deferred): needs a `git_sha → PR` resolver primitive (e.g., a new `resolve_pr` op on `repo_helper` wrapping `GET /repos/{owner}/{repo}/commits/{sha}/pulls`) plus threading the commit SHA through eval result records. Tracked as a 9b follow-up.

### 9c — Improvement Proposer + A/B routing ✅

- [x] `packages/common/src/common/routing.py` — `pick_variant(run_id, agent_name)` returns `"a"` or `"b"` via stable SHA-256 hash; `load_system_prompt(agent, variant)` imports `{agent}.prompts_b` when variant is `b` and the module exists, else falls back to `{agent}.prompts`. 7 unit tests.
- [x] Every Strands agent (`architect`, `critic`, `reviewer`, `tester`) plus the Implementer's `options.py` updated: `build_agent(run_id)` resolves the variant per call. Falls back to `prompts.py` when no `prompts_b.py` exists, so the routing is dormant by default — adding a B variant is a single PR adding the file.
- [x] `agents/proposer/` — Strands + Opus 4.7. Reads rejection histogram (from telemetry), drift report (from 9b), eval pass-rate aggregate, few-shot example counts, and `MEMORY.md`. Output is a `Proposal` with one or more `FileEdit`s; **the Pydantic validator restricts target files to `docs/MEMORY.md` or `agents/*/src/*/prompts(_b)?.py`** — blast radius is bounded at the model level, not just at code review. App orchestrates branch creation + commit + PR open via `repo_helper` (installation-token path; PRs attribute to `ai-dlc[bot]`).
- [x] `proposer` registered in `var.agents` (`targets = ["repo_helper"]`); ECR repo + CI matrix + per-repo runtime env (`AIDLC_REPO_HELPER_FUNCTION_NAME`) wired.
- [x] Per-runtime IAM extended with a `DirectInvokeToolLambdas` statement gated on `targets`, so agents that orchestrate tool Lambdas directly (vs. via the gateway) get only the Lambdas in their declared targets.
- [x] Two trigger paths: (a) `aws_scheduler_schedule.proposer_weekly` (Mondays 09:00 UTC); (b) `proposer_trigger` Lambda subscribed to the alerts SNS topic — invokes the Proposer with `trigger_reason="regression"` when the eval-regression alarm fires.

**Data sample policy:** 100% of rejected runs, 10% of approved (full prompt + output). DSAR redaction needed if user data flows in.

**Out of scope for v1:** auto-applied prompt rewrites; third-party prompt-optimization frameworks (DSPy/MIPRO) — revisit after 9c has proven the loop.

---

## Phase 10 — Team v1 (Critic + Reviewer + Tester) 🟡

Three new pipeline-gate agents that every run flows through. All advisory in v1: they emit informational events; the HITL gate still owns `SPEC.APPROVED` / `TASK.APPROVED`. Promote any of them to gating only after observing real runs.

```
Receive
  → Architect (spec)
  → Critic              ← NEW (Opus 4.7, advisory)
  → HITL: spec approval
  → for each task:
      → Implementer (code → PR)
      → Reviewer        ← NEW (Sonnet 4.6, advisory)
      → Tester          ← NEW (Haiku 4.5, advisory)
      → HITL: task approval
  → Completed
```

- [x] `packages/common/src/common/events.py` — 3 new event types: `CRITIQUE.READY`, `REVIEW.READY`, `TEST_REPORT.READY` plus payload classes; `AnyPayload` union extended.
- [x] `packages/common/src/common/runtime.py` — 3 new contract pairs: `CriticInput`/`Result`, `ReviewerInput`/`Result`, `TesterInput`/`Result`.
- [x] `agents/critic/` — Strands + Opus 4.7. Reads spec from S3, produces a structured `Critique`, uploads `runs/{run_id}/critique.md`.
- [x] `agents/reviewer/` — Strands + Sonnet 4.6. Code-reviews each task PR with verdict + comments; uploads `runs/{run_id}/tasks/{task_id}/review.md`.
- [x] `agents/tester/` — Strands + Haiku 4.5. Identifies test gaps + suggests Given/When/Then tests; uploads `runs/{run_id}/tasks/{task_id}/test_report.md`.
- [x] Terraform `agents` map extended with 3 new entries (model IDs + tool target sets); `registry` ECR repos extended; `pipeline` locals + ASL template + IAM policy updated.
- [x] ASL: `InvokeCritic` inserted between `InvokeArchitect` and `PublishSpecReady`; `InvokeReviewer` + `InvokeTester` (serial) inserted inside the `IterateTasks` Map ItemProcessor between `InvokeImplementer` and `PublishTaskReady`. `WaitForSpecApproval` carries `critique_s3_key`; `WaitForTaskApproval` carries `review_verdict`/`review_comment_count`/`test_gap_count` so the HITL gate has the advisory signals at hand.
- [x] `images-build.yml` matrix extended to `[architect, critic, implementer, reviewer, tester]`.
- [x] `docs/eval-set/cases.yaml` pass-criteria schema extended with `critique_present`, `review_present`, `test_report_present` (all default true in v1 — the agents always run).
- [ ] Live AWS smoke (gated only on the live-AWS apply now that `repo_helper` network calls have shipped): a single run produces critique + review + test-report artifacts in S3 and the dashboard timeline shows the 3 new event types per task.

**Cost delta** (rough, per run): spec phase ~2× (1 Opus → 2 Opus); task phase ~2.4× per task (1 Sonnet → 1 Sonnet + 1 Sonnet + 1 Haiku). Latency adds ~30s spec phase, ~90s per task. Acceptable given the quality lift; revisit if real runs show a problem.

**Out of scope (Phase 10b+):**
- Out-of-pipeline peer agents beyond the Proposer (Researcher, Debugger, TechWriter) — separate phase, different trigger model.
- Promoting Critic/Reviewer/Tester to gating with `*.REJECTED` events.
- Parallel execution of Reviewer + Tester (Step Functions `Parallel` state).
- A registry-driven ASL (declarative agent sequencing) — only justifies itself when the team grows past ~6 agents.
- Per-agent token-cost split on the dashboard run-detail panel (extends the existing `STATE` row from Phase 8).

---

## Phase 11 — On-behalf-of GitHub auth 🟡

So commits, PR opens, and PR comments attribute to the human who submitted the run rather than to `ai-dlc[bot]`. Bot attribution remains the fallback for system-driven runs (eval suite, scheduled Proposer, runs without a linked user).

```
User → Cognito sign-in → Dashboard → "Connect GitHub" (one time)
                                       │
                                       ▼
                         AgentCore Identity USER_FEDERATION on GithubOauth2
                                       │
                                       ▼
                       Token Vault stores user GitHub OAuth token (keyed by Cognito sub)

Run submission carries `requestor_sub` (Cognito sub) + `target_repo` →
Step Functions threads them through Architect → Critic → Implementer/Reviewer/Tester →
each agent that touches GitHub fetches the user's token via
`GetWorkloadAccessTokenForUserId` + `GetResourceOauth2Token(USER_FEDERATION)`.
```

### 11a — Plumbing + Reviewer/Tester OBO ✅

- [x] `packages/common/src/common/events.py` — `RequestReceived.requestor_sub` (Cognito sub) + `target_repo` (`owner/name`). Both nullable for system-driven runs.
- [x] `packages/common/src/common/runtime.py` — same fields added to every agent input (`Architect/Critic/Implementer/Reviewer/Tester`).
- [x] `lambdas/repo_helper/src/repo_helper/auth.py` — refactored from `requestor_jwt` (a credential) to `requestor_sub` (an identifier) using `bedrock-agentcore:GetWorkloadAccessTokenForUserId`. JWTs no longer flow through events / state-machine input — only the stable Cognito sub does.
- [x] `terraform/modules/pipeline/asl/sdlc.asl.json.tftpl` — threads `requestor_sub` + `target_repo` through `Receive → Architect → Critic → IterateTasks ItemSelector → Implementer/Reviewer/Tester` payloads.
- [x] `services/dashboard/src/dashboard/routes/runs.py` + `models.py` + `templates/submit.html` — submit form gains a `target_repo` input; `submit_run` populates `requestor_sub` from `user.sub`.
- [x] `agents/reviewer/src/reviewer/app.py` + `agents/tester/src/tester/app.py` — invoke `repo_helper.comment_pr` via Lambda invoke after producing the review/report. Forwards `requestor_sub`; advisory (failure never blocks the run).

### 11b — Implementer commits as user ✅

- [x] `agents/implementer/src/implementer/agentcore_auth.py` — fetches the user's GitHub OAuth token from AgentCore Identity (`GetWorkloadAccessTokenForUserId` + `GetResourceOauth2Token`). Falls back to `AIDLC_GITHUB_TOKEN` env (installation token) when no requestor is linked.
- [x] `agents/implementer/src/implementer/repo_ops.py` — replaced the env-var-based auth with a per-invocation `RepoSession` that holds: target_repo, access token, author_login + author_email (resolved via `GET /user`), and an `on_behalf_of_user` flag. `clone_repo`, `open_pr`, etc. all take the session. `configure_git_author` writes `user.name` + `user.email` from the session into the freshly-cloned repo so commits attribute to the requestor.
- [x] `agents/implementer/src/implementer/client.py` — builds the session at run start; logs whether the run is on-behalf-of-user or bot-attributed; passes the session through every git/GitHub call.
- [x] `terraform/modules/agents/runtime.tf` — runtime IAM gains the `AgentCoreUserObo` statement (gated on `targets` containing `repo_helper` and `var.github_app != null`); env vars `AIDLC_GITHUB_OAUTH_PROVIDER_NAME` + `AIDLC_AGENT_WORKLOAD_NAME` set on the same condition. Implementer's runtime role now has everything it needs.

### 11c — Dashboard "Connect GitHub" OAuth flow ✅

- [x] `services/dashboard/src/dashboard/routes/auth_github.py` — two routes:
  - `GET /auth/github` calls `bedrock-agentcore:GetWorkloadAccessTokenForUserId` (with `user.sub`) + `GetResourceOauth2Token(USER_FEDERATION, scopes=[])`. If AgentCore returns an `accessToken` (already linked) → renders "linked". Otherwise sets the `aidlc_obo_session_uri` cookie (10-min TTL, secure + httponly + lax) and redirects to the returned `authorizationUrl`.
  - `GET /auth/github/callback` reads the cookie, calls `complete_resource_token_auth(userIdentifier={"userId": user.sub}, sessionUri=...)`, deletes the cookie, renders the success page.
- [x] `services/dashboard/src/dashboard/templates/connect_github.html` — page covering all four states: linked, just-linked, missing-session, callback-failed.
- [x] `templates/base.html` nav — "connect github" link.
- [x] `services/dashboard/src/dashboard/deps.py` — `Settings` carries `dashboard_workload_name` + `github_oauth_provider_name`.
- [x] `terraform/modules/agents/identity.tf` — adds `aws_bedrockagentcore_workload_identity.dashboard` (gated on `github_app != null`); `outputs.tf` exposes its name.
- [x] `terraform/modules/dashboard/data.tf` + `ecs.tf` + `variables.tf` — task IAM gains `bedrock-agentcore:GetWorkloadAccessTokenForUserId` / `GetResourceOauth2Token` / `CompleteResourceTokenAuth`; container env carries `AIDLC_DASHBOARD_WORKLOAD_NAME` + `AIDLC_GITHUB_OAUTH_PROVIDER_NAME`.
- [x] `terraform/envs/dev/main.tf` — wires `module.agents.dashboard_workload_name` + `module.agents.github_oauth_provider_name` into the dashboard module.
- [ ] Live smoke: install the GitHub App on a real repo, sign into the dashboard, click "Connect GitHub", complete authorization on github.com, submit a run, observe the Implementer commit + Reviewer/Tester PR comments attributed to the user.

---

## Phase 12 — Autonomous, issue-driven flow 🟡

The platform's first eleven phases assume a human kicks off each run via the dashboard's `POST /v1/runs`. Phase 12 makes it self-driving: a tagged GitHub issue is the trigger; the system decides whether to proceed, ask, defer, or decline; PRs are the human review surface (no separate dashboard approval step for routine work); and production PR feedback closes the loop back into prompt and `MEMORY.md` updates.

```
GitHub issue assigned to @aidlc-bot
  → Triage agent (Haiku 4.5)
      ├─ proceed → ASL Choice on workflow_kind
      │             ├─ spec_driven → existing Architect → Critic → Map(impl/review/test) flow
      │             ├─ bug_fix     → reproduce → fix → test
      │             ├─ upgrade     → scan → bump → test
      │             └─ docs        → single-agent edit
      ├─ ask     → comment questions on issue, wait for reply, re-triage
      ├─ defer   → comment, leave for human, stop
      └─ decline → comment + close
  → Implementer opens PR
      ├─ TWO_WAY  → ready for review; merge on green review
      └─ ONE_WAY  → draft; human marks ready before merge
  → GitHub webhooks
      → pr_telemetry  → comment_classifier  → eval_aggregator
                                                  └─ drift detected
                                                       → Proposer (PRs against prompts / MEMORY.md)
```

**Door taxonomy** (committed list — see `packages/common/src/common/door.py`): the ten ONE-WAY categories are `schema_migration`, `public_api_break`, `production_terraform`, `iam_authorization`, `auth_flow`, `cryptography_or_secrets`, `major_dependency_bump`, `scheduled_job`, `event_schema_breaking`, `public_deletion`. Detection is layered: Architect emits `door` per task; Critic and Reviewer can upgrade; a PreToolUse hook on `open_pr` enforces a path-based hard floor; ONE-WAY PRs open as `gh pr create --draft`.

**GitHub primitives, no new tags**: trigger = issue assigned to `@aidlc-bot`. Issue Type (Bug / Feature / Task) hints `workflow_kind`. Existing labels are read as informational context. Unassigning the bot stops in-flight work cleanly.

Six landings, each independently shippable:

### 12a — Typed contracts ✅

- [x] `packages/common/src/common/door.py` — `DoorClass`, 10-category `OneWayCategory`, `DoorAssessment` (with cross-field validation), `classify_paths()` for the 7 path-detectable categories.
- [x] `packages/common/src/common/triage.py` — `WorkflowKind`, `TriageAction`, `MissingInformation`, `TriageDecision` with consistency validators across `action × workflow_kind × missing_information`.
- [x] `packages/common/src/common/eval.py` — `CommentCategory` (10), `AgentOwner`, `COMMENT_WEIGHT` table, `ClassifiedComment`, `PRTelemetry`, `EfficiencyMetrics`, `DriftSignal`.
- [x] `agents/architect/src/architect/spec.py` — `Task` gains `door: DoorAssessment` (default `two_way`) and `depends_on: list[str]`; `render_tasks` surfaces ONE-WAY door class and dependencies.
- [x] 50 new unit tests (22 door / 11 triage / 12 eval / 5 architect); ruff and ty clean; full suite green.

### 12b — Triage agent + ASL branching + issue-comment HITL 🟡 (code-side complete; Terraform + ASL + dispatcher rewire deferred to live-AWS apply)

- [x] `agents/triage/` skeleton — `pyproject.toml` (workspace member), `src/triage/{__init__.py, agent.py, prompts.py, decision.py}`. Strands + Haiku 4.5; `Agent.structured_output(TriageDecision, …)`. `compose_message(payload)` builds the user prompt from a `TriageInput`. `decision.py` re-exports `TriageDecision` from `common.triage` for a stable agent-scoped import path. **5 unit tests**.
- [x] `packages/common/src/common/runtime.py` — `TriageInput` (issue context: url, number, title, body, type, labels, prior triage rounds, prior human comments) and `TriageResult` (flattened `action` + `workflow_kind` + `decision_s3_key` for Step Functions Choice branching).
- [x] `agents/triage/{app.py, Dockerfile}` — `BedrockAgentCoreApp` entrypoint validates `TriageInput`, calls `triage_issue`, uploads the decision JSON to `s3://{artifacts_bucket}/runs/{run_id}/triage.json`, returns `TriageResult`. Multi-stage uv ARM64 Dockerfile mirrors the Critic pattern.
- [x] Two new event types: `ISSUE.TRIAGED`, `ISSUE.ASK_POSTED` in `common/events.py` + JSON schemas under `terraform/shared/schemas/`. **5 new event tests**.
- [ ] GitHub webhook subscriptions: `issues.assigned` (trigger) and `issue_comment.created` on issues (resume the *ask* path). Existing `triage_dispatcher` Lambda already accepts webhooks; needs to expand its trigger filter and rewire its classifier.
- [ ] `lambdas/triage_dispatcher/` — replace the existing Bedrock-Converse classifier with a thin shim that invokes the triage runtime; one component, one responsibility. Defers because the runtime ARN is set by Terraform.
- [x] Terraform: registered `triage` in `var.agents` (`targets = []` — agent talks to S3 directly via runtime IAM, no gateway tools needed); added to registry's `repositories` + `agentcore_pull_repositories`; added to dev composition's `local.agent_image_tags` and `module.agents.agents`; added to `images-build.yml` matrix. `terraform validate` clean. **Live-AWS apply on next push.**
- [ ] ASL: new `Triage` state at the top; `Choice` on `decision.action` + `decision.workflow_kind`; new ItemProcessors for `bug_fix`, `upgrade`, `docs` workflows. The *ask* branch uses `.waitForTaskToken` resolved by the issue-comment webhook. **Live-AWS apply gate.**
- [ ] First pass ships `bug_fix` / `upgrade` / `docs` as no-spec variants of the existing pipeline; richer per-phase agent ensembles can come later.

### 12c — One-way door enforcement ✅ (12 unit tests; ready_for_review webhook moves to 12d)

- [x] `agents/implementer/src/implementer/repo_ops.py` — `open_pr` gains a `draft: bool = False` parameter; classifies the actual diff via `common.door.classify_paths` and forces draft mode when any one-way path is touched. New `changed_paths(base)` runs `git diff --name-only origin/{base}...HEAD`. Logs a warning when the override engages. (The original plan called for a Claude SDK PreToolUse hook on `open_pr`, but Claude doesn't call `open_pr` as an MCP tool — the Python harness does after the agent finishes; enforcement therefore lives in the harness function itself.)
- [x] Architect persona (`agents/architect/src/architect/prompts.py`): new operating principle #8 — emit `door` per task with the ten one-way categories enumerated, and the rule that `one_way` requires both `categories` and a one-sentence `rationale`. Default stays `two_way`.
- [x] Critic persona (`agents/critic/src/critic/prompts.py`): "Door audit" added to the failure-mode hunt list — file `high`-severity issue when a task is marked `two_way` but its scope falls into one of the ten categories. Calls out content-only categories (`public_api_break`, `major_dependency_bump`, `public_deletion`) as the agents' responsibility since the path classifier can't catch them.
- [x] Implementer's explanatory PR comment (`repo_ops.draft_explanation`, `comment_on_pr`): when the draft override engages, posts a follow-up comment listing the detected categories and instructing the maintainer to mark the PR "Ready for review" before merge.
- [x] Reviewer persona (`agents/reviewer/src/reviewer/prompts.py`): "Door re-audit" added to the failure-mode hunt list — same 10 categories, focused on the content-only check the path classifier can't perform.
- [ ] Webhook: subscribe to `pull_request.ready_for_review`; record `marked_ready_at` + `marked_ready_by` in `PRTelemetry`. (Moved to 12d alongside the rest of the PR webhook subscriptions.)

### 12d — Production efficiency eval 🟡 (3 Lambdas landed code-side; proposer rewire + Terraform pending)

Phase 9b detects regressions on the synthetic eval-set (10 cases, nightly). Phase 12d adds a complementary signal: real PR comments on real merged PRs. Same proposer, different signal source.

- [x] `lambdas/pr_telemetry/` — webhook handler for `pull_request` (opened, closed, ready_for_review), `pull_request_review` (submitted), `pull_request_review_comment` (created), and `issue_comment` on PRs (created). Recognises platform PRs by the `_run_id: <uuid>_` marker the Implementer writes in the PR body footer; ignores third-party PRs in the same repo. Increments per-PR counters atomically (`requested_changes_count`, `review_count`, `comment_count_human`, `comment_count_bot`); records `marked_ready_at`/`marked_ready_by` on `ready_for_review`; flips `merged` + records `merged_at`/`closed_at` on close. **11 unit tests** under moto-backed DDB.
- [x] `lambdas/comment_classifier/` — Bedrock Haiku Converse-API call categorises one review comment into the 10 `CommentCategory` values; falls back to `unclear` on Bedrock failure or unparseable JSON; persists `ClassifiedComment` JSON to `s3://artifacts/evals/classified_comments/{date}/{pr_slug}/{comment_id}.json`. **11 unit tests** with mocked Bedrock + moto S3.
- [x] `lambdas/eval_aggregator/` — scheduled aggregator. Pure-function `aggregate.py` rolls `PRTelemetry` rows into per-bucket `EfficiencyMetrics` (commitment C6 grain: `(target_repo, agent_owner, prompt_variant)`); applies the C1 friction-score weights; respects the C1 ONE-WAY-PR carve-out from `merge_as_is_rate`. Drift detection applies the C4 rule (≥20% friction-score delta vs 30-day baseline AND ≥10 PRs). On drift, emits `EVAL.DRIFT_DETECTED` events to the platform bus. **19 unit tests** on the pure-function aggregator. New event type added to `EventType` literal + JSON schema in `terraform/shared/schemas/EVAL_DRIFT_DETECTED.json`. `DriftSignal` lifted to inherit from `common.events.Payload` so it slots into `EventEnvelope[DriftSignal]`.
- [ ] Proposer: subscribe to `EVAL.DRIFT_DETECTED` (in addition to its existing eval-regression alerts); read recent low-efficiency PRs + comment categories; open PRs against `docs/MEMORY.md` or agent prompts (existing `Proposal` validator already restricts target files).
- [ ] Dashboard: per-bucket "efficiency over time" view (extends the existing run-detail / metrics surface).
- [ ] Terraform: new DDB telemetry table; new Lambda module (or extension to `improvement`) for the three Lambdas; webhook subscription on the dashboard ALB → pr_telemetry. **Live-AWS apply gate.**

### 12e — Persona refinements 🟡 (taxonomy rename to `critical/important/suggestion/nitpick` deferred — see note)

Per the comparison with `bug-ops/claude-plugins/rust-code` agent personas (lifted generically — none of the Rust-specific bits).

- [x] Critic 8-dimension framework — operating principle #7 in `agents/critic/src/critic/prompts.py`: assumption audit, counterexample hunt, scalability stress, failure-mode analysis, alternative hypotheses, completeness check, dependency risk, second-order effects. Plus principle #8 (severity rule): *a finding that does not threaten the task goal cannot be `high`*.
- [x] Reviewer + Tester severity discipline — operating principle #8 added to both prompts. The wire-format taxonomy stays `high / medium / low` (changing the Pydantic literal would cascade into `CriticResult`, `ReviewerResult`, `TesterResult`, `CritiqueReady`, `ReviewReady`, `TestReportReady`, JSON schemas, dashboard counters). The behavioural intent — `low` is suggestions/nits, the human reviewer can ignore — is enforced via the prompt update; full rename to `critical/important/suggestion/nitpick` is parked as its own contract-migration slice.
- [x] Coordination footer on all seven agent prompts (Architect, Critic, Implementer, Proposer, Reviewer, Tester, Triage) — predecessor / expected context / focus.
- [x] `packages/common/src/common/personas/` — shared persona snippets (`DOOR_TAXONOMY`, `MEMORY_MD_DISCIPLINE`, `PR_PROSE_VOCABULARY_BAN`, `coordination_footer`). **5 unit tests**.

### 12f — Security & supply-chain hardening 🟡 (UserPromptSubmit MEMORY.md pre-injection deferred)

Lifted from Trail of Bits' `claude-code-config` patterns where they apply.

- [x] Operator's `~/.claude/settings.json` template provided at `docs/operator/claude-settings.json` + `docs/operator/README.md`: read-block `~/.aws/**`, `~/.git-credentials`, `~/.docker/config.json`, `~/.kube/**`, `~/.ssh/**`, `~/.gnupg/**`; PreToolUse compound-command regex for `rm -rf` and `git push origin (main|master)` (with proper handling of `;`, `&&`, `||`, `|`); `enableAllProjectMcpServers: false`. Operator merges by hand into their existing settings.
- [x] PR-prose vocabulary ban added to Implementer prompt and Proposer prompt — banned words: `critical`, `crucial`, `essential`, `significant`, `comprehensive`, `robust`, `elegant`.
- [x] Implementer hooks tightened (`agents/implementer/src/implementer/hooks.py`): bare-substring deny-list replaced with compiled regex with word boundaries — `\bterraform\s+apply\b` no longer denies a write to a doc that happens to contain the path-string `terraform/apply.tf`; `.env.example` is now writable while `.env` is denied. New entry: `gh pr create` is denied (PRs always go through `repo_ops.open_pr` so the path-classifier safety net runs).
- [x] Audit-log PostToolUse hook — `audit_log_writes` appends one JSONL row per mutating tool call (`Write`, `Edit`, `Bash`, `NotebookEdit`) to `$AIDLC_AUDIT_LOG_PATH` (default `/workspace/audit.jsonl`). Audit-log failures never block the agent. **33 new unit tests** covering parameterised positive / negative cases for every deny pattern + audit-log lifecycle.
- [ ] `pyproject.toml`: verify all transitive deps pinned to `==`; document `uv pip install --require-hashes` install path. (`pip-audit` already runs in CI per Phase 0.)
- [ ] Implementer `UserPromptSubmit` analog that pre-injects `MEMORY.md` so reading it is non-optional. (Deferred — needs Claude Agent SDK `UserPromptSubmit` support landing or a workaround via a tool-call gate.)

**Out of scope (Phase 12+):**
- Stacked PRs (PRs in a sequence opening simultaneously with `depends on #X` markers). Sequential merging is the v1 default.
- Auto-merge for TWO-WAY PRs on green review without any human looking. v1 still posts the PR; humans can choose to enable repo-level auto-merge if they want.
- Triage agent training data / fine-tuning. v1 is prompt-driven against Haiku 4.5.
- Multi-tenant memory namespace migration tooling (the contract is multi-tenant from day one via `target_repo`; no migration is needed for fresh deployments).

---

## Status

**Test totals**: 176 unit tests pass across `packages/common`, all six agents, all eight Lambdas, and the dashboard. `ruff check`, `ruff format --check`, `ty check`, and `terraform validate` are all green.

Phases 0-8 have their main deliverables in place — the platform stands up via `terraform apply` (modulo first-time bootstrap), the agents are container-buildable, and the spec-driven pipeline + dashboard are wired end-to-end. Phases 9 (continuous improvement loop), 10 (team v1: Critic + Reviewer + Tester), and 11 (on-behalf-of GitHub auth) are all in flight; their main code deliverables are landed, and live-AWS smoke is the remaining gate on each.

**Terraform lifecycle note**: the `agents` module gates AgentCore Runtime creation on `var.agent_image_tags` — only agents listed there get a runtime. Add a new agent to `var.agents` first (creates IAM / gateway / workload identity), push the image via `images-build`, then add the agent to `agent_image_tags` and re-apply. This avoids the `aws_ecr_image` data source failing on agents whose first image hasn't been built yet.

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
