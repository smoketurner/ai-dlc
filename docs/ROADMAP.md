# Roadmap

Live tracker for the AI-DLC build. The architectural reference is [`aws-agent-architecture-guide.md`](aws-agent-architecture-guide.md); the orchestration design lives in [`SQS-DESIGN.md`](SQS-DESIGN.md).

The platform's seven agents (Architect, Critic, Implementer, Reviewer, Tester, Triage, Proposer), the FastAPI dashboard, and the SQS-beacon + DDB-state orchestration are all in place. Step Functions and the four legacy orchestration Lambdas (`hitl_handler`, `runtime_invoker`, `iteration_reactor`, `triage_dispatcher`) have been removed in the SQS cutover. The eval pipeline (state machine + drift detector + GitHub Actions workflow) was also removed — `docs/eval-set/` cases remain as reference for when we rebuild it.

**Current focus:** none — the deploy-readiness audit is closed. The platform is ready to ship to production.

Legend: ✅ done · 🟡 in progress · ⬜ todo

---

## Pipeline shape

```
REQUEST.RECEIVED
  → ISSUE.TRIAGED        (Triage classifies issue-driven runs — Haiku 4.5)
  → SPEC.READY           (Architect — Opus 4.7)
  → CRITIQUE.READY       (Critic — Opus 4.7, advisory)
  → SPEC.APPROVED        (gate 1 — human merges the spec PR)
  → TASK.READY           ┐
  → REVIEW.READY         │ Reviewer — Sonnet 4.6, advisory
  → TEST_REPORT.READY    │ Tester — Haiku 4.5, advisory
  → TASK.APPROVED        │ loop while tasks remain — one PR per task
  → ...                  ┘
  → RUN.COMPLETED
```

The state cursor (`RunState` / `TaskState` enums in `common.state`) is advanced by the `event_projector` Lambda applying transitions from `common.state_transitions`. The `state_router` Lambda reads the cursor off the runs DDB table on each SQS-beacon delivery and dispatches the next side-effect (agent invoke, `repo_helper` call, event emit). The router never advances state on its own initiative for "what happened" transitions — those go through the projector.

---

## SQS cutover stabilization ✅

Behavioural gaps the SFN→SQS cutover left behind. All shipped:

- [x] Architect / Critic / Triage agents emit their completion events (`common.event_emit.publish`) — `architect_running` / `critic_running` / `triaging` were dead-end states without these.
- [x] Shared `common.runs.start_run(...)` helper used by all three entry paths (entry_adapter, dashboard `/v1/runs`, webhook): writes the STATE row, emits `REQUEST.RECEIVED`, sends the SQS beacon.
- [x] `handle_spec_approved` seeds one TASK row per task before transitioning to `tasks_in_progress`. `SpecReady` carries `task_ids` so the seeder doesn't need to fetch the spec from S3.
- [x] State router opens spec PRs via `repo_helper` op `open_spec_pr` (compound: read 3 S3 docs → branch → commit → PR).
- [x] `target_repo`, `intent`, full issue context (`issue_number`, `issue_title`, `issue_body`, `issue_labels`) persisted on the STATE row at trigger time so the router can rebuild agent payloads without re-reading GitHub.
- [x] Projector extracts `workflow_kind` + `triage_action` from `ISSUE.TRIAGED` so `handle_triage_decided` branches correctly on `proceed` / `ask` / `defer` / `decline`.
- [x] `gsi_pr` populated: router writes `pr_url` on STATE row when opening the spec PR; projector writes `pr_url` on TASK rows when applying `TASK.READY`. Webhook resolves PRs via this index — no PR-body marker parsing.
- [x] Webhook idempotency on `X-GitHub-Delivery` for issue-driven mints (Powertools `idempotent_function`). `project_slug` derived consistently via `slug_from_repo` across all entry paths.
- [x] `parse_run` falls back to deriving `run_id` from `pk = "RUN#{id}"` when the attribute is missing on projector-created rows.
- ~~State advance triggers a fresh beacon.~~ Rejected — would create multiple in-flight beacons per run, violating the single-beacon-per-run design. The 60s visibility-timeout recycle is the cost of the pattern.

---

## Pre-deploy hardening ✅

Audit findings against [`SQS-DESIGN.md`](SQS-DESIGN.md). All shipped.

### Blockers (silent data loss or cost leakage) ✅

- [x] **Projector clears `pending_feedback` and `delivery_ids` on `TASK.READY` from `iterating`.** `advance_task_state` now appends `SET pending_feedback = :empty_list REMOVE delivery_ids` for the iteration-complete branch via the new `apply_task_ready_clauses` helper.
- [x] **Projector accumulates iteration feedback when state can't advance.** `apply_task_state_transition` calls a new `accumulate_iteration_in_place` when `TASK.ITERATION_REQUESTED` arrives in `iterating` / `implementer_running` — the conditional update appends feedback + delivery_id without changing state.
- [x] **`dispatch_advisors` race-protected.** New `GuardedAdvance` action gates the `pr_open → pending_approval` transition; only the winning router runs `on_success` and fires reviewer + tester. `InvokeAgent.advance_*` made optional so the gated invokes fire unconditionally. Verified by `lambdas/state_router/tests/test_executor.py`.

### Correctness gap ✅

- [x] **Dashboard terminal detection reads `current_state`, not `status`.** `services/dashboard/src/dashboard/repos.py` exposes `TERMINAL_STATES` (derived from `common.state.TERMINAL_RUN_STATES`) and a new `is_run_terminal(run_id)` helper; SSE stream and `delete_run` use the state-machine cursor. `RunSummary` carries `current_state` for templates. Cancelled and rejection-induced terminal runs now close the stream, are deletable, and render the terminal badge.

### Cleanup ✅

- [x] **Deleted unused Lambda source** (`lambdas/eval_aggregator`, `lambdas/comment_classifier`, `lambdas/pr_telemetry`) and the now-orphaned `common.eval` module + tests. `pyproject.toml` `known-first-party` and `pricing.py` comment trimmed.
- [x] **Removed never-emitted event types** `TASK.ITERATION_STARTED`, `TASK.ITERATION_COMMITTED`, `TASK.MAX_ITERATIONS_REACHED`, `ISSUE.ASK_POSTED` (and the `IterationTriggerKind` literal that fed them) from `common.events`, `terraform/modules/messaging/locals.tf`, `terraform/shared/schemas/`, and tests.
- [x] **Removed `hitl_enabled` from `common.settings`** + corresponding test assertions.
- [x] **Removed `approvals:write` Cognito scope** from `terraform/modules/auth/locals.tf`.
- [x] **Refreshed stale docstrings** in `agents/{architect,critic,triage}/app.py` and `terraform/modules/pipeline/locals.tf` so they describe the SQS-beacon orchestration. Removed the dead `local.runtime_arns` (replaced by `state_router_runtime_arns`).

### Out of scope for this round

- Stuck-run detector schedule. SQS-DESIGN risk #2 — a beacon loss leaves a run stuck silently. Add a CloudWatch alarm on `non_terminal_runs_with_stale_last_event > 0` until the schedule is built.
- Reviewer/Tester parallelism (currently sequential via `dispatch_advisors`).
- Per-run cost cap.
- Auto-merge for TWO-WAY PRs.
- Rebuilding the eval pipeline (`cases.yaml` + 10 case docs kept as reference).

---

## Deferred (no concrete trigger yet)

- Switch AgentCore Runtime to VPC mode.
- Migrate to AgentCore Harness when GA.
- A2A protocol for cross-team or third-agent invocation.
- Slack-based HITL approvals.
- Playwright E2E tests for the dashboard.
- Custom domain for the dashboard.
- Tighten alarm thresholds against real dev traffic.

---

## Decided not to do

Don't re-propose without a trigger that wasn't true at the time of the decision.

- ~~Cedar / Verified Permissions for cross-agent RBAC~~ — per-agent IAM + resource tags are sufficient.
- ~~Langfuse / Datadog OTEL backend~~ — CloudWatch is the trace backend.
- ~~Multi-account AWS Org / Control Tower~~ — single account with env separation is the plan.
- ~~`common/personas/` shared snippet module~~ — used by zero callers; agent prompts have inline equivalents.

---

## How to use this file

1. When you finish a checkbox, mark it `[x]` in the same PR that contains the change.
2. When a section is fully checked, update its header from 🟡 to ✅.
3. Work that comes out of execution but doesn't belong to the current focus: drop it in **Deferred**, ideally with a corresponding GitHub issue.
4. Don't gold-plate. Promote items from Deferred only when there's a concrete trigger.
