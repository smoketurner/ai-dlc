# Roadmap

Live tracker for the AI-DLC build. The architectural reference is [`aws-agent-architecture-guide.md`](aws-agent-architecture-guide.md); the orchestration design lives in [`SQS-DESIGN.md`](SQS-DESIGN.md).

The platform's seven agents (Architect, Critic, Implementer, Reviewer, Tester, Triage, Proposer), the FastAPI dashboard, and the SQS-beacon + DDB-state orchestration are all in place. Step Functions and the four legacy orchestration Lambdas (`hitl_handler`, `runtime_invoker`, `iteration_reactor`, `triage_dispatcher`) have been removed in the SQS cutover. The eval pipeline (state machine + drift detector + GitHub Actions workflow) was also removed — `docs/eval-set/` cases remain as reference for when we rebuild it.

**Current focus:** closing the per-state behavioural gaps the cutover left behind. The structural rewrite landed; what's missing is what the old SFN ASL embedded at each step — agent event emission, task seeding, spec PR opening, the issue-driven entry path.

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

## SQS cutover stabilization 🟡

The cutover replaced SFN with SQS beacon + DDB state but didn't reproduce every behaviour the old ASL had at each state. End-to-end trace from `aidlc-bot` being assigned to a GitHub issue shows where things dead-end:

```
GitHub issue assigned to @aidlc-bot
  ↓
dashboard webhook → emit REQUEST.RECEIVED       [no row written, no beacon sent]
  ↓
projector → write {status, project_slug, source_issue_url},
            advance current_state="received"
  ↓
[router never wakes — no beacon was ever sent]

  ⚠ even if a beacon eventually arrived:
state_router → handle_received → invoke_triage  [missing target_repo / issue_number / title / body]
  ↓
triage agent runs → returns TriageResult        [no event emitted — SFN PublishIssueTriaged is gone]
  ↓
[stuck at triaging forever]
```

Ordered by blast radius. Each batch is independently shippable.

### Blockers (any happy path needs these) ✅

- [x] **Architect / Critic / Triage agents emit their completion events.** Mirror Implementer/Reviewer/Tester (which emit via `common.event_emit.publish` after building the result). Today these three return the result and rely on a SFN `PublishXxxReady` state that no longer exists; `architect_running`, `critic_running`, `triaging` are dead-end states.
- [x] **Webhook + dashboard `/v1/runs` write the run STATE row + send the SQS beacon.** Currently both only emit `REQUEST.RECEIVED`. Issue-driven and UI runs never wake the router. Extract a shared `common.runs.start_run(...)` helper used by all three entry paths (entry_adapter, dashboard /v1/runs, webhook).
- [x] **Task rows seeded after spec approval.** `handle_spec_approved` returns `AdvanceState`; needs to seed one TASK row per task before transitioning to `tasks_in_progress`. Otherwise `tasks_in_progress` with an empty task list is a permanent Noop.
- [x] **`SpecReady` event carries `task_ids`.** Add the field to the payload + Architect's emit code; without it the seeder has to fetch the spec from S3.
- [x] **State router can open spec PRs.** Today it calls `op="open_spec_pr"` which doesn't exist on `repo_helper`. Either add a compound op (read 3 S3 docs → `create_branch` → `commit_files` → `open_pr`) or have the router orchestrate those primitives directly.

### Issue-driven path 🟡

- [ ] **`target_repo` persisted on STATE row.** `entry_adapter`'s `RunRequest` model accepts it; webhook writes it; dashboard already has it. Otherwise the Architect can't clone and the Implementer can't commit.
- [ ] **`intent` persisted on STATE row** for issue-driven and dashboard runs. Architect requires `min_length=1`.
- [ ] **`invoke_triage` carries full issue context.** Persist `issue_number`, `issue_title`, `issue_body`, `issue_labels` on the run STATE row when the webhook writes it; pass them through in the router's triage payload. `TriageInput` requires all of these.
- [ ] **`workflow_kind` persisted on `ISSUE.TRIAGED`.** Projector's `update_run_state` extracts it from the payload. Without this, `handle_triage_decided` falls into the `spec_driven` default for every run, even bug_fix/upgrade/docs.
- [ ] **`handle_triage_decided` handles `action ∈ {ask, defer, decline}`.** Today only `proceed` is handled. The old `triage_dispatcher` posted issue comments + labels for `ask`; marked the issue `aidlc:declined` / `aidlc:deferred` for the others.

### PR matching 🟡

- [ ] **`gsi_pr` GSI gets populated.** State router writes `pr_url` (not `spec_pr_url`) on STATE rows when opening the spec PR; projector writes `pr_url` on TASK rows when applying `TASK.READY`. The webhook's `lookup_pr` returns nothing today because the attribute is never written.
- [ ] **Webhook idempotency.** Use Powertools `idempotent_function` keyed on `X-GitHub-Delivery` so a re-delivered event doesn't mint a duplicate `run_id`.
- [ ] **`project_slug` consistent across entry paths.** Webhook currently uses `repo.split("/", 1)[-1]` (just the name). Switch to `slug_from_repo` (lowercase + `/` → `-`) so the same repo gets the same slug from every entry path.

### Robustness 🟡

- [ ] **`parse_run` falls back to pk parsing for `run_id`.** Projector-created rows don't set the `run_id` attribute explicitly; should derive from `pk = "RUN#{id}"` when the attribute is missing.
- [ ] **TASK row creation precedes `TASK.READY`.** Either seed the TASK row before dispatching the implementer, or have the projector upsert the row on `TASK.READY` arrival rather than skipping silently.
- [ ] **State advance triggers a fresh beacon.** Current 60s visibility-timeout cadence adds up to 60s of latency to every state transition. Have the projector send a `DelaySeconds=0` beacon on each transition; or the router self-enqueues a short-delay follow-up after each AdvanceState.

**Out of scope for this round:**

- Reviewer/Tester parallelism (currently sequential via `dispatch_advisors`).
- Per-run cost cap.
- Stuck-run detector schedule.
- Rebuilding the eval pipeline (`cases.yaml` + 10 case docs kept as reference).
- Auto-merge for TWO-WAY PRs.

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
