# Cost fixes — duplicate dispatches + implementer retry loop

**Status:** Plan / not started.
**Origin:** Live test on issue [smoketurner/ai-dlc#33](https://github.com/smoketurner/ai-dlc/issues/33), runs `019e045c-…` and `019e04e8-…`.

Two cost issues surfaced during end-to-end validation of the blocked-PR
flow. Fixing them is essential before we put real-world traffic on this
system; left unfixed, every run pays roughly **2× the necessary tokens**
on every agent stage and an unbounded multiple on the implementer when
something goes wrong upstream.

This document captures the diagnosis and a concrete plan for each.

---

## Item 1 — Duplicate agent invocations on every dispatch

### What we observed

Every agent stage (triage, architect, critic, implementer, …) runs the
agent **twice** for a single beacon. Two invocations 2-4 seconds apart,
with **different `requestId`s but the same `sessionId`**, on a single
SQS message. Examples from run `019e04e8-…`:

| Agent | First requestId | Second requestId | Spread |
|---|---|---|---|
| triage | `681fccd8-…` | `eea78858-…` | 3.1s |
| architect | (similar pattern) | | |
| critic | (similar pattern) | | |
| implementer | (similar pattern) | | |

### What we ruled out

- **State-router did not double-dispatch.** CloudWatch shows exactly one
  Lambda invocation, one `BeaconsProcessed` metric, one
  `dispatch_to_runtime` call per agent stage.
- **SQS did not double-deliver.** `NumberOfMessagesReceived` for the
  state-router queue was 1.0 across the relevant minute.
- **boto3 client-side retry is disabled.** `runtime_client()` in
  `lambdas/state_router/src/state_router/handler.py:86` configures
  `Config(retries={"max_attempts": 1, "mode": "standard"})`.
- **No second beacon enqueue.** `entry_adapter`, projector, and webhook
  paths each enqueue exactly one beacon per run-state advance.

### Most likely cause

The state-router's `dispatch_to_runtime` deliberately uses a 2-second
client-side `read_timeout`; the agent runs much longer than that, so
the call hits `ReadTimeoutError`, the state-router catches it, treats
it as "successful dispatch," and returns. **The TCP connection closes
mid-flight on the AgentCore Runtime side.**

AgentCore Runtime's endpoint (or whatever fronts it — most likely an
ALB / API Gateway managed by AWS) appears to interpret the dropped
client connection as a delivery failure and **retries the dispatch to
a backend container**. The retry produces a fresh `requestId` but
keeps the same `sessionId`, and lands on a (possibly different) warm
container instance. Net result: the agent runs twice for one
intentional dispatch.

### Fix

Stop relying on `ReadTimeoutError` as the success signal. Two options:

1. **Bump the read-timeout to 30s.** AgentCore's `InvokeAgentRuntime`
   API returns a `200` well before the agent finishes its work
   (seconds, not minutes — the framework hands off to the container
   and returns). With a 30s timeout, the dispatch returns a real
   response, the client closes cleanly, and the runtime has no reason
   to retry.

   - **Effort:** ~30 minutes (one-line change to
     `DISPATCH_READ_TIMEOUT_SECONDS`, plus a regression test that the
     state-router still treats sub-30s responses as success).
   - **Cost impact:** state-router Lambda is billed for the wait. In
     practice the response should come back well under 1s, so per-
     dispatch billed duration goes from ~2s to maybe ~1-2s. *Far*
     cheaper than running every agent twice.
   - **Risk:** if AgentCore takes longer than 30s to acknowledge, we
     get a real timeout and the rollback fires — the existing path.

2. **Switch to an async invoke.** Verify whether
   `bedrock-agentcore:InvokeAgentRuntime` supports an `InvocationType:
   Async` (or equivalent) parameter. If yes, the call returns
   immediately and AgentCore handles dispatch internally — no retry
   on connection drop.

   - **Effort:** ~1 hour (mostly verification).
   - **Risk:** unverified — may not exist. If it does and the contract
     differs from the current "fire-and-forget on read-timeout" assumption, more work is needed.

**Recommendation:** start with (1). It's a one-line change, has known
semantics, and addresses the immediate cost. Verify AgentCore's async
support as a follow-up if Lambda billing becomes a concern.

### Forks / open questions

- Does AgentCore Runtime support an explicit async invocation
  parameter? (Verify against the latest `bedrock-agentcore` SDK.)
- The 2s timeout was chosen so the state-router doesn't hold a Lambda
  for the agent's full runtime. With (1), state-router holds the
  Lambda for ~1-2s instead of 2s. Is that within Lambda concurrency
  budget? (Dev's been fine at 0.04 RPS; haven't sized for prod.)

---

## Item 2 — Implementer retry loop on dispatch-rollback

### What we observed

The blocked-path validation surfaced a separate bug in
`checkout_task_branch` (now fixed in `agents/implementer/src/implementer/repo_ops.py`).
Before the fix shipped, the iteration path hit the bug, the agent
container raised `RuntimeError` in <1s, AgentCore returned 5xx to the
state-router, `dispatch_to_runtime` returned `False`,
`rollback_invoke_advance` reverted `implementer_running → iterating`,
the SQS beacon redelivered, and the cycle repeated **every minute**.

Each cycle:
- billed Lambda time on the state-router and projector,
- spun up an implementer container that ran for ~0.5s before crashing,
- left no visible signal beyond the implementer's CloudWatch error log.

The user's only escape was to manually purge the SQS queue.

### Why the existing design didn't catch it

`docs/SQS-DESIGN.md` mentions a stuck-run detector that "sends a fresh
beacon (in case the original was lost) AND raises a CloudWatch
metric" — but that detector handles the *opposite* failure mode (lost
beacon, run sitting still). It has no concept of *too many* dispatch
attempts, and no mechanism to stop a dispatch loop short of manual
queue purge.

### Fix

A circuit breaker on consecutive dispatch-rollback cycles per task /
run. Concrete design:

#### State

New attributes on each `TASK` and `RUN` row:

- `dispatch_failure_count` (Number, default 0).
- `last_dispatch_failure_at` (ISO timestamp, optional — for ops
  triage).

#### Increment

In `state_router.handler.rollback_invoke_advance`, atomically
increment `dispatch_failure_count` in the same `update_item` that
reverses the state advance. The increment is conditional on the
state successfully rolling back (avoids double-counting on retry).

#### Check

In `state_router.handler.invoke_advance_succeeds`, before the
conditional state advance, read the current `dispatch_failure_count`.
If `>= MAX_DISPATCH_FAILURES` (suggested: **3**), do **not** advance,
do **not** dispatch — instead, emit a circuit-breaker event:

- For tasks: `TASK.BLOCKED` with `blocked_reason="dispatch failed
  N times — circuit breaker tripped"`. The existing blocked-path
  flow takes over: a draft PR is opened (or the existing one is
  updated), the human can comment to retry or close to abort.
- For runs (no associated task): `RUN.FAILED` with a clear
  `error_class="dispatch_circuit_open"` and the recent error from
  CloudWatch (or just a pointer to the log group).

#### Reset

When the agent successfully completes a stage (any `*.READY` event
arrives at the projector), the projector resets
`dispatch_failure_count` to 0 alongside the state advance. The reset
lives in `event_projector.handler.advance_task_state` (and the run
equivalent) — same `update_item` that flips the cursor.

#### Why this shape

- **No new state machine**, just a counter alongside the existing
  state cursor. Same DDB row, same conditional update pattern.
- **Reuses the blocked-path UX** for tasks: humans see the failure
  through GitHub PR review, not a dashboard the system doesn't
  trust yet.
- **Bounded cost**: at most `MAX_DISPATCH_FAILURES` agent invocations
  per task per "stuck" condition. With N=3 the worst case is 3
  failed implementer runs (~1.5s each) before the circuit opens —
  a few cents instead of unbounded.
- **Self-healing on ack**: a successful run resets the counter, so
  intermittent infrastructure blips don't gradually fill the budget.

### Effort

~3-5 hours.

- `common/state_transitions.py` — no change (no new task states).
- `event_projector` — handle `dispatch_failure_count` reset on
  `*.READY` events; tests for reset.
- `state_router` — increment on rollback, check on advance, emit
  circuit-breaker event when tripped; tests for each path.
- `common/events.py` — extend `TaskBlocked` payload to allow the
  circuit-breaker reason (already a free-form string, no schema
  change needed).
- `terraform/modules/messaging/locals.tf` — no change.
- ROADMAP entry to remove the existing implementer-dead-end follow-up
  (this fix supersedes it).

### Forks / open questions

- **N value.** 3 is a guess; could be 5 to give intermittent
  recovery a wider window, or 2 to fail faster. Suggested starting
  point: 3.
- **Per-task vs per-run.** Per-task is finer-grained (one stuck task
  doesn't kill the whole run); per-run is simpler. Suggested: per-task.
- **Reset on partial progress.** If the implementer dispatches
  successfully (state advances to `implementer_running`) but the
  agent then dies later in its own run (e.g., a hang detected by
  AgentCore), do we reset the counter or keep it? Suggested: reset
  on dispatch-success, since "the dispatch worked" is what the
  counter measures, not "the agent finished."
- **Observability.** Should the counter be surfaced as a CloudWatch
  metric per increment so we can alarm on growing-but-not-tripped
  counters? Suggested: yes, dimension by `agent`.

---

## Sequencing

1. **Item 1** first — it's a one-line code change with a clear
   diagnosis, and every successful run is paying 2× until it's
   fixed. Lowest-risk highest-leverage change in the doc.
2. **Item 2** second — multi-file but well-scoped. Required before
   shipping any traffic that doesn't have a human babysitting the
   queue.
3. After both, remove the existing follow-up
   "Correctness gap — implementer dead-ends on no-diff" from
   `docs/ROADMAP.md` (Item 2 supersedes it).

## What does NOT change

- The agents themselves, their prompts, or the events they emit.
- The projector's transition table (already covers `TaskState.blocked`).
- The blocked-PR UX — the circuit breaker reuses it for "I gave up
  trying to dispatch."
