# SDLC orchestration redesign — SQS beacon + DDB state + EventBridge events

**Status:** Design proposal. Replaces the Step Functions SDLC pipeline + iteration_reactor side-loop.

## Why

The current architecture (`docs/aws-agent-architecture-guide.md`) puts AWS Step Functions Standard at the centre of the SDLC pipeline, with a per-task Map iterating Implementer → Reviewer → Tester → human gate. We bolted a separate `iteration_reactor` Lambda on top of it to handle PR-iteration triggers.

That design is wrong for the actual problem shape:

1. **SDLC isn't a DAG of tasks — it's a state machine with cycles, indefinite waits, and external events.** SFN expresses workflows as directed graphs with linear progression. PR iteration loops back to a prior state, multi-day human reviews exceed the `waitForTaskToken` 7-day ceiling, and webhook-driven events arrive asynchronously throughout.
2. **Adding a state requires editing JSON-template ASL, applying Terraform, redeploying.** For a fleet that evolves, the friction is wrong.
3. **Iteration is a special case bolted on**, not a primitive. The `iteration_reactor` exists because SFN couldn't model the "PR open, awaiting events that may re-invoke the implementer" pattern naturally.
4. **Cost.** SFN Standard charges per state transition. At fleet scale this dominates.
5. **HITL gates use `waitForTaskToken`** with a 7-day timeout. Real PRs sit longer.

## What we're building

A code-driven state machine where:

- **Run state lives in DynamoDB** (atomic, queryable, replayable).
- **EventBridge carries observations** — agent completions, webhook events, user actions.
- **Event projector advances DDB state** when events arrive (idempotent on `event_id`).
- **One SQS "beacon" message per active run** acts as a distributed lease + work-pending signal.
- **A single `state_router` Lambda** long-polls SQS, reads the run's current state from DDB, and dispatches whatever the next action is — invoke an agent, open a PR, mark a run done. The router never advances state; it only acts on it.
- **Agents stay AgentCore Runtime workers**, invoked async (fire-and-forget) by the router and emitting their completion events to EventBridge.

The architecture has clean separation:

| Layer | Job | Component |
|---|---|---|
| **Observation** | "What happened?" | EventBridge events |
| **State** | "What's true now?" | DynamoDB run + task rows |
| **Coordination** | "Which run needs attention?" | SQS beacon queue |
| **Action** | "What do I do given current state?" | `state_router` Lambda |
| **Work** | "Do the SDLC step" | Agents on AgentCore Runtime |

## Architecture diagram

```
┌─────────────────────┐
│ GitHub webhooks +   │
│ API GW /v1/runs +   │      writes  ┌─────────────────┐
│ Dashboard /v1/runs  │─────────────▶│ DynamoDB        │
└─────────────────────┘     enqueue  │ runs table      │
          │                          │ (state)         │
          │ emits                    └─────────────────┘
          ▼                              ▲
┌─────────────────────┐                  │ updates
│ EventBridge bus     │ ───events──▶ ┌──────────────────┐
│ (custom)            │              │ event_projector  │
└─────────────────────┘              │ (Lambda)         │
          ▲                          └──────────────────┘
          │ emit on done
          │
┌─────────────────────┐                                      ┌──────────────────┐
│ Agents              │◀───── async invoke ──────────────────│ state_router     │
│ (AgentCore Runtime) │                                      │ (Lambda)         │
└─────────────────────┘                                      │                  │
                                                             │ long-polls SQS,  │
                                                             │ reads DDB,       │
                                                             │ dispatches.      │
                                                             │ NEVER advances   │
                                                             │ state.           │
                                                             └──────────────────┘
                                                                    ▲
                                                                    │ beacon poll
                                                                    │
                                                             ┌──────────────────┐
                                                             │ SQS state-router │
                                                             │ queue (no DLQ)   │
                                                             │                  │
                                                             └──────────────────┘
```

## State enum

Run-level state. Flat enum, no hierarchical nesting — keeps the dispatch table trivially readable.

```python
class RunState(StrEnum):
    # Triage phase (issue-driven runs only)
    received           = "received"            # webhook accepted, run row written
    triaging           = "triaging"            # triage agent in flight
    triage_decided     = "triage_decided"      # triage emitted decision; router branches on workflow_kind

    # Spec phase (spec_driven workflow only)
    spec_pending       = "spec_pending"        # ready to dispatch architect
    architect_running  = "architect_running"
    spec_drafted       = "spec_drafted"        # SPEC.READY arrived; router dispatches critic next
    critic_running     = "critic_running"
    spec_critiqued     = "spec_critiqued"      # CRITIQUE.READY arrived; router opens spec PR
    spec_pr_open       = "spec_pr_open"        # spec PR is open, awaiting human merge
    spec_approved      = "spec_approved"       # spec PR merged (or auto-merged)

    # Tasks phase
    tasks_in_progress  = "tasks_in_progress"   # one or more tasks active; per-task rows track sub-state
    tasks_complete     = "tasks_complete"      # all tasks merged

    # Terminal
    done               = "done"
    failed             = "failed"
    cancelled          = "cancelled"
```

Per-task state. Lives on `pk=RUN#{run_id}, sk=TASK#{task_id}` rows. The router's `tasks_in_progress` handler walks all task rows.

```python
class TaskState(StrEnum):
    pending             = "pending"             # not yet dispatched
    implementer_running = "implementer_running" # agent in flight
    pr_open             = "pr_open"             # PR opened, awaiting CI / advisors / human
    reviewer_running    = "reviewer_running"
    tester_running      = "tester_running"
    iterating           = "iterating"           # implementer re-invoked for a fix commit
    pending_approval    = "pending_approval"    # all signals received, awaiting human merge
    merged              = "merged"
    closed              = "closed"
    failed              = "failed"
```

## Dispatch table

The state_router's core. Keyed on `(run_state, optional task_state)`. Each handler returns either a list of side-effects (invoke agent, open PR, etc.) or a no-op signal.

```python
DISPATCH = {
    RunState.received:           handle_received,            # → invoke triage
    RunState.triaging:           noop,                       # waiting for ISSUE.TRIAGED
    RunState.triage_decided:     handle_triage_decided,      # branch on workflow_kind
    RunState.spec_pending:       handle_spec_pending,        # → invoke architect
    RunState.architect_running:  noop,                       # waiting for SPEC.READY
    RunState.spec_drafted:       handle_spec_drafted,        # → invoke critic
    RunState.critic_running:     noop,
    RunState.spec_critiqued:     handle_spec_critiqued,      # → open spec PR (auto-merge if clean)
    RunState.spec_pr_open:       noop,                       # waiting for PR webhook
    RunState.spec_approved:      handle_spec_approved,       # → seed task rows + advance to tasks_in_progress
    RunState.tasks_in_progress:  handle_tasks_in_progress,   # walk task rows; dispatch any actionable
    RunState.tasks_complete:     handle_tasks_complete,      # → emit RUN.COMPLETED
    RunState.done:               terminal,
    RunState.failed:             terminal,
    RunState.cancelled:          terminal,
}

TASK_DISPATCH = {
    TaskState.pending:             dispatch_implementer,
    TaskState.implementer_running: noop,                     # waiting for TASK.READY
    TaskState.pr_open:             dispatch_advisors,        # parallel reviewer + tester
    TaskState.reviewer_running:    noop,
    TaskState.tester_running:      noop,
    TaskState.iterating:           noop,                     # waiting for TASK.READY (re-emitted)
    TaskState.pending_approval:    noop,                     # waiting for PR webhook
    TaskState.merged:              terminal,
    TaskState.closed:              terminal,
    TaskState.failed:              terminal,
}
```

Adding a state = adding an entry. No ASL edit, no Terraform apply for orchestration changes.

## Event-to-state map

The event_projector subscribes to all `ai-dlc.*` events and applies state transitions atomically (DDB conditional updates on `current_state` + `last_event_id` for idempotency).

```python
# (event_type, expected_current_state) → next_state
TRANSITIONS = {
    ("REQUEST.RECEIVED",       None):                    RunState.received,
    ("ISSUE.TRIAGED",          RunState.triaging):       RunState.triage_decided,
    ("SPEC.READY",             RunState.architect_running): RunState.spec_drafted,
    ("CRITIQUE.READY",         RunState.critic_running): RunState.spec_critiqued,
    ("SPEC.APPROVED",          RunState.spec_pr_open):   RunState.spec_approved,
    ("SPEC.REJECTED",          RunState.spec_pr_open):   RunState.failed,
    # task-level transitions on TASK rows, keyed similarly
    ("TASK.READY",             TaskState.implementer_running): TaskState.pr_open,
    ("REVIEW.READY",           TaskState.reviewer_running):    TaskState.pr_open,  # advisory only
    ("TEST_REPORT.READY",      TaskState.tester_running):      TaskState.pr_open,
    ("TASK.APPROVED",          TaskState.pending_approval):    TaskState.merged,
    ("TASK.REJECTED",          TaskState.pending_approval):    TaskState.closed,
}
```

Critical invariant: **only the projector writes to `current_state`.** The router reads but never writes state. This keeps the state machine deterministic and gives us a single source of all transitions for audit.

## DDB schema

Reuses the existing `runs` table — no new resources.

**Run row** (`pk=RUN#{run_id}, sk=STATE`):

| Attribute | Type | Purpose |
|---|---|---|
| `current_state` | S (RunState enum) | The state machine cursor |
| `phase` | S | Coarse grouping for dashboards (`triage`, `spec`, `tasks`, `done`) |
| `project_slug` | S | |
| `intent` | S | |
| `workflow_kind` | S | `spec_driven` / `bug_fix` / `upgrade` / `docs` |
| `target_repo` | S | `owner/name` |
| `requestor_sub` | S? | Cognito sub for OBO commits |
| `source_issue_url` | S? | If triggered from a GH issue |
| `spec_slug` | S? | Set after architect completes |
| `spec_pr_url` | S? | Set when spec PR opens |
| `task_ids` | SS | Set after architect completes |
| `last_event_id` | S | For projector idempotency |
| `last_event_at` | S (ISO ts) | For stuck-run detection |
| `state_transitions` | N | Optimistic concurrency counter |
| `usage_token_in/out`, `usage_cost_usd`, `usage_duration_ms` | N | Accumulated from agent events (existing) |

**Task row** (`pk=RUN#{run_id}, sk=TASK#{task_id}`):

| Attribute | Type | Purpose |
|---|---|---|
| `status` | S (TaskState enum) | Per-task cursor |
| `pr_url`, `pr_number` | S/N? | Set when implementer opens PR |
| `iteration_count` | N | 0 for initial run, ++ per iteration |
| `delivery_ids` | SS | Webhook idempotency for iteration triggers |
| `last_event_id`, `last_event_at` | S, S | Same pattern as run row |

DDB streams are already enabled — no schema migration needed.

## SQS beacon contract

Single queue, one message per active run.

| | |
|---|---|
| Queue name | `${prefix}-state-router` |
| DLQ | none — beacons cycle indefinitely until terminal |
| Visibility timeout | 60s |
| Long-polling wait | 20s |
| `maxReceiveCount` | unset (no redrive policy) |
| Message body | `{"run_id": "<uuid7>"}` |
| Lifecycle | Created on `REQUEST.RECEIVED`. Deleted by Lambda's SQS event source mapping when the handler returns it as a successful record (terminal / orphan / malformed). |

The SQS message is **only a beacon** — it carries no state and triggers no work directly. The router's job on each receive is "read DDB; if there's a next action, do it." After dispatch, the handler reports the message as a batch-item failure (`function_response_types=["ReportBatchItemFailures"]`), which keeps it visible after the visibility timeout. The state machine ticks at this cadence until the run reaches a terminal state.

No DLQ on purpose: SQS caps `maxReceiveCount` at 1000, which would push a beacon to a DLQ after 1000 × 60s ≈ 16 hours regardless of whether the workflow is healthy. Active runs waiting on a multi-day spec-PR human merge would silently die. Real failures (validation errors, dispatch failures) surface via CloudWatch alarms on receive-count age or invocation-error metrics, not DLQ-by-redelivery.

## State router behaviour

```python
def handler(sqs_event, _ctx):
    for record in sqs_event["Records"]:
        run_id = json.loads(record["body"])["run_id"]
        run = read_run(run_id)
        if run is None:
            delete_message(record)               # orphan; drop
            continue
        if run.current_state in TERMINAL_STATES:
            delete_message(record)               # done; drop
            continue
        action = decide(run)                     # may return Noop
        if action is not None:
            action.execute()
        # Do NOT delete on no-op or successful dispatch.
        # Visibility timeout expires → next poll picks it up.

def decide(run: Run) -> Action | None:
    if run.current_state == RunState.tasks_in_progress:
        return decide_tasks(run)                 # walks task rows
    return STATE_HANDLERS[run.current_state](run)
```

Critical properties:

- **Conditional dispatch.** Each handler that invokes an agent does a DDB conditional update advancing the state to `*_running` BEFORE invoking. Only one router instance wins the race; the loser sees the new state on the next poll and no-ops.
- **Fire-and-forget agent invocation.** Same 2-second-read-timeout pattern as the existing `runtime_invoker`. Router doesn't block on agent completion; agent emits a completion event when done.
- **Idempotent handlers.** All handlers must be safe to call twice (network retries, double-delivery). The `*_running` state guard handles most cases; per-task `delivery_ids` set handles webhook re-delivery for iteration triggers.

## Webhooks become event publishers

The dashboard's `services/dashboard/src/dashboard/routes/webhooks.py` stops invoking handler Lambdas. It just translates GitHub events into EventBridge events:

| GitHub event | Emits |
|---|---|
| `pull_request.closed merged=true` (spec PR) | `SPEC.APPROVED` |
| `pull_request.closed merged=false` (spec PR) | `SPEC.REJECTED` |
| `pull_request.closed merged=true` (task PR) | `TASK.APPROVED` |
| `pull_request.closed merged=false` (task PR) | `TASK.REJECTED` |
| `pull_request_review.submitted state=approved` | `TASK.APPROVED` |
| `pull_request_review.submitted state=changes_requested` | `TASK.ITERATION_REQUESTED` (new) |
| `pull_request_review_comment.created` w/ bot mention | `TASK.ITERATION_REQUESTED` |
| `issue_comment.created` w/ bot mention on PR | `TASK.ITERATION_REQUESTED` |
| `issue_comment.created` w/ `/aidlc cancel` | `RUN.CANCEL_REQUESTED` (new) |
| `workflow_run.completed` (failure) | `TASK.ITERATION_REQUESTED` |
| `issues.opened/labeled/assigned` (triage triggers) | `REQUEST.RECEIVED` |
| `issues.unassigned` (cancel) | `RUN.CANCEL_REQUESTED` |
| `issue_comment.created` w/ `/aidlc go` or on awaiting-response issue | `REQUEST.RECEIVED` |

The projector applies these to DDB state. The router picks up the beacon and dispatches.

**Implication:** No more `gate_ref` PR-body parsing for HITL routing — the projector matches PRs to runs/tasks via `pr_url` lookup on the runs/tasks tables (add a GSI on `pr_url` if needed).

## Lambda audit — what stays, modifies, deletes

| Lambda | Verdict | Why |
|---|---|---|
| `entry_adapter` | **MODIFY** | Still receives POST /v1/runs. Now writes the run DDB row + sends the beacon, in addition to emitting `REQUEST.RECEIVED`. |
| `event_projector` | **MODIFY** | Already projects events to DDB + AgentCore Memory. Extend to apply `TRANSITIONS` map atomically. The projector becomes the only writer of `current_state`. |
| `repo_helper` | **KEEP** | Agents need GitHub API access. Unchanged. |
| `artifact_tool` | **KEEP** | Agents need S3 + MEMORY.md ops. Unchanged. |
| `comment_classifier` | **KEEP** | Eval-pipeline only. Independent of orchestration. |
| `pr_telemetry` | **KEEP** | Independent telemetry sink subscribed to PR webhooks. |
| `telemetry` | **KEEP** | Categorises rejections from `SPEC.REJECTED` / `TASK.REJECTED`. Still useful. |
| `drift_detector` | **KEEP** | Eval regression detection, separate pipeline. |
| `eval_runner` | **MODIFY** | Currently uses `startExecution.sync:2` against the SDLC SFN. Switch to: publish `REQUEST.RECEIVED` with `actor_id="eval-runner"`, then poll DDB run row for `current_state in {done, failed}`. |
| `eval_aggregator` | **KEEP** | Independent rollup. |
| `few_shot_miner` | **KEEP** | Independent. |
| `hitl_handler` | **DELETE** | Task tokens are gone. Webhook → event → projector → state advance is the new HITL path. |
| `runtime_invoker` | **DELETE** | The state router invokes agents directly with the same fire-and-forget pattern (2s read timeout). |
| `iteration_reactor` | **DELETE** | Iteration is a state transition (`pr_open → iterating → pr_open`), handled by the state router. Replaces ~250 LoC of side-loop with ~10 LoC in the dispatch table. |
| `triage_dispatcher` | **DELETE** | Triage is a state (`received → triaging → triage_decided`). The state router invokes the triage agent. The synthetic-spec-write path moves into `handle_triage_decided` for non-`spec_driven` workflows. |
| **NEW: `state_router`** | **ADD** | Long-polls SQS, reads DDB, dispatches. ~200–300 LoC. |

**Net delta:** −4 lambdas, +1 lambda. ~700 lines of bolted-on iteration code (the `iteration_reactor` + supporting pieces) collapses into ~30 lines in the dispatch table.

## Step Functions deletion

Once the router is live and validated:

- Delete `terraform/modules/pipeline/state_machine.tf`.
- Delete `terraform/modules/pipeline/asl/sdlc.asl.json.tftpl`.
- Delete the `aws_cloudwatch_event_rule.request_received` → `start_sdlc` target wiring.
- Delete the `events_to_sfn` IAM role.
- Update `terraform/modules/improvement/eval_state_machine.tf` to remove the SDLC SFN dependency (point eval runner at the new beacon-based entry).

## Deletions in `packages/common/`

- `common.task_token` module — no more `SendTaskSuccess` / `SendTaskFailure` callbacks.
- `task_token: str | None` field on `ImplementerInput`, `ReviewerInput`, `TesterInput`.
- The `if payload.task_token is not None:` branches in `agents/{implementer,reviewer,tester}/src/*/app.py`. Agents always run async-without-token; they always emit completion events themselves.
- `common.event_emit.publish` becomes the single emission path for ALL agents (already exists).

## Deletions in DDB schema

- `approvals` table — delete entirely. Gates are DDB run-row state, not task tokens.
- The iteration_count + delivery_ids columns from the `iteration_reactor`'s sk-namespace stay (now on TASK rows under their natural home).

## Deletions in agent code

- `iteration_reactor` invocation paths.
- The `gate_ref` marker in PR body footers (no longer parsed by the webhook for HITL routing — projector matches PRs by URL).
- The duplicated event-emission code in reviewer/tester `app.py` (move to a single `agent_complete()` helper in `common.event_emit`).

## Stuck-run detector

Add a small EventBridge schedule (`rate(15 minutes)`) → tiny Lambda or even an EventBridge-Pipes-driven query that:

1. Scans the runs table for non-terminal runs with `last_event_at < now - 1h`.
2. For each, sends a fresh beacon (in case the original was lost) AND raises a CloudWatch metric.
3. Optionally pages or auto-cancels after 24h with no progress.

Cheap insurance against beacon loss + observability into stuck runs.

## Migration plan

**Phase 1 — additive (safe, parallel to existing pipeline):**
- Define state enum + dispatch table in code (no behaviour change).
- Add `state_router` Lambda with full dispatch logic but DON'T wire it to anything yet.
- Add SQS beacon queue (no DLQ — see "SQS beacon contract" above).
- Add `RUN.CANCEL_REQUESTED` and `TASK.ITERATION_REQUESTED` event types.
- Extend `event_projector` to apply state transitions on a feature-flagged second code path.
- Modify `entry_adapter` to ALSO send beacon (in addition to existing event emit).
- Tests at every layer — unit tests for the dispatch table, integration tests with moto.

**Phase 2 — cutover:**
- Disable EventBridge rule `request_received` → SFN `StartExecution`.
- Enable SQS event source mapping for `state_router`.
- Webhook handler stops invoking `hitl_handler` and `iteration_reactor`; emits events instead.
- Run a real SDLC end-to-end on a fixture repo. Verify spec PR open, merge, task PRs, iteration, merge.

**Phase 3 — cleanup (only after Phase 2 stabilises in dev):**
- Delete the 4 lambdas: `hitl_handler`, `runtime_invoker`, `iteration_reactor`, `triage_dispatcher`.
- Delete the SFN state machine + ASL.
- Delete `approvals` DDB table.
- Delete `task_token` plumbing across `common` and the three task-phase agents.
- Delete `gate_ref` PR-body marker.
- Update `eval_runner` to use the new entry path.
- Update GitHub App webhook subscriptions if needed (`workflow_run`, `pull_request_review_comment` already wired from the iteration reactor work).

**Phase 4 — polish:**
- Custom dashboard view: state machine visualiser per run.
- Stuck-run detector schedule.
- Per-project knobs: disable advisor re-runs on iteration (cost cap), customize iteration budget.
- Consider cancellation UX (PR label, magic comment, dashboard button) — all just emit `RUN.CANCEL_REQUESTED`.

## Risks

1. **Loss of SFN console.** Genuine downgrade for debugging. Mitigation: invest in the dashboard state-machine view in Phase 4. Until then, CloudWatch Logs Insights queries on the router + projector are the debug surface.
2. **Beacon loss.** SQS guarantees at-least-once delivery, but if a deploy or queue config error wipes a beacon, the run sits in DDB forever. Mitigation: stuck-run detector (Phase 4) re-injects beacons.
3. **Polling cost.** Bounded by `(active_runs × duration) / visibility_timeout × poll_cost`. At 100 active runs averaging 1h with 60s visibility = ~6,000 polls/hour ≈ $0.0024/hr ≈ $50/year. Negligible.
4. **State machine bugs.** A mistake in the dispatch table (e.g., dispatching the same agent twice on a state) is harder to spot than a mistake in ASL. Mitigation: exhaustive unit tests on `decide()` + integration test that drives every state transition end-to-end.
5. **Cutover risk.** Big change, multi-system. Phase 1's additive strategy keeps the SFN running until the new path is proven; cutover is a single EventBridge rule flip.
6. **Eval pipeline.** Currently uses `states:startExecution.sync:2`. Migrating to "publish event + poll DDB for terminal" requires careful handling of long-running cases (eval timeout vs SDLC duration).

## What does NOT change

- **All seven agents** keep their AgentCore Runtime deployment, their Strands/Claude SDK loops, their prompts, their MCP tool wiring. Only the dispatch path into them changes.
- **Dashboard FastAPI service** is unchanged for the user — `/v1/runs` POST, `/v1/runs/{id}/stream` SSE, `/v1/runs/{id}` GET — all read from the same runs table.
- **AgentCore Memory** integration via `event_projector` is unchanged.
- **EventBridge platform bus** schemas — only adds new event types.
- **DDB runs + idempotency_keys tables** keep their existing schema; we add new sk-namespaces.
- **GitHub App** webhook subscriptions are unchanged from what we wired for the iteration reactor work.

## Open questions for design review

1. **Should `state_router` be a Lambda or a long-running ECS task?** Lambda is simpler (one function, scales automatically). ECS gives true long-polling without per-invoke overhead. For SDLC volumes, Lambda is the right call — revisit if you ever need >1000 concurrent runs.
2. **One beacon per run, or one beacon per active task within a run?** Single beacon per run keeps coordination simple (one consumer at a time per run) but serializes per-run task dispatch. Multi-task PRs in parallel still work because each dispatch is fast (fire-and-forget). Recommendation: **one beacon per run** for v1.
3. **Should we keep `TASK.ITERATION_STARTED` / `TASK.ITERATION_COMMITTED` / `TASK.MAX_ITERATIONS_REACHED` events?** They're now redundant — state transitions are the source of truth. Recommendation: keep them as observability sugar (dashboard timeline) but stop using them for control flow.
4. **Where does the synthetic-spec-write logic for `bug_fix` / `upgrade` / `docs` workflows live?** Today it's in `triage_dispatcher`. In the new model, recommendation: a small `handle_triage_decided` branch in the state router writes the synthetic spec to S3 inline before transitioning to `tasks_in_progress`.
5. **Cancellation semantics.** `RUN.CANCEL_REQUESTED` arrives — projector transitions to `cancelled`. But agents may still be in flight. Do we wait? Try to cancel via `bedrock-agentcore:StopAgentRuntime`? Recommendation: transition to `cancelling` substate, attempt agent stop (best-effort), transition to `cancelled` after completion-or-timeout. Document that in-flight agent calls run to completion.

## Summary

Replace SFN orchestration with: DDB as state, EventBridge as observation, SQS beacon as coordination, single state_router Lambda as dispatch. Delete 4 Lambdas (~1500 LoC). Add 1 Lambda (~250 LoC). Keep all 7 agents, the dashboard, and 8 of the 15 existing Lambdas unchanged.

Net result: an orchestration layer that fits the actual problem shape (state machine with cycles, indefinite waits, external events), evolves at code-velocity, scales cheaply, and treats iteration as a primitive instead of a special case.
