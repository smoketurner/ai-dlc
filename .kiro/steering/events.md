# Event Schema & Orchestration

EventBridge custom bus with schema registry drives all coordination. Every platform event uses a versioned envelope so routing is possible without parsing the payload.

## EventEnvelope Structure

```python
class EventEnvelope[PayloadT]:
    schema_version: Literal["1.0"]
    event_id: EventId          # UUID7
    type: EventType            # the discriminator
    timestamp: datetime        # UTC, tz-aware
    run_id: RunId              # UUID7
    correlation_id: CorrelationId
    causation_id: EventId | None
    actor_id: str              # e.g. "architect-a", "state_router"
    payload: PayloadT          # typed per event_type
```

The envelope sits inside the EventBridge `detail` field. The bus's own `source` and `detail-type` are derived from `actor_id` and `type` at publish time. The platform could move off EventBridge without rewriting any model code.

Three envelope variants exist:
- `EventEnvelope` -- strict, used at emission time
- `IncomingEnvelope` -- permissive (`strict=False`), used at ingestion to coerce ISO-8601 strings to datetimes
- `UntypedEnvelope` -- carries envelope metadata with an untyped `dict` payload, used when a Lambda operates on payloads structurally

## Event Types

All event types from the `EventType` literal:

### External Triggers

| Type | Description |
|------|-------------|
| `REQUEST.RECEIVED` | New run entered the system (via webhook or dashboard) |
| `IMPL.ITERATION_REQUESTED` | Human `@aidlc-bot` mention on the impl PR |
| `VALIDATION.REQUESTED` | Human asked for validator re-run (`@aidlc-bot review`) |
| `CHECKS.PASSED` | All required GitHub Checks green for PR HEAD sha |
| `CHECKS.FAILED` | One or more required Checks failed for PR HEAD sha |

### Dispatch Markers (Idempotency Proof)

| Type | Description |
|------|-------------|
| `TRIAGE.DISPATCHED` | Router invoked the Triage agent |
| `ARCHITECT.DISPATCHED` | Router invoked the Architect agent |
| `IMPLEMENTER.DISPATCHED` | Router invoked the Implementer (initial or revision) |
| `VALIDATORS.DISPATCHED` | Router invoked reviewer + tester + code-critic |
| `PROPOSER.DISPATCHED` | Router invoked the Proposer agent |

### Agent Results

| Type | Description |
|------|-------------|
| `ISSUE.TRIAGED` | Triage classified the issue (carries `action` verdict) |
| `DESIGN.READY` | Architect wrote `plan.md` to S3 |
| `IMPL_PR.OPENED` | Implementer opened the impl PR |
| `REVIEW.READY` | Reviewer code-reviewed the PR (carries `verdict`) |
| `TEST_REPORT.READY` | Tester flagged test gaps |
| `CODE_CRITIQUE.READY` | Code-Critic adversarially reviewed the PR |
| `REVISION.READY` | Implementer applied fixes and pushed |

### Terminal

| Type | Description |
|------|-------------|
| `RUN.COMPLETED` | Run reached terminal success (PR merged) |
| `RUN.FAILED` | Run reached terminal failure (cap hit, dispatch error) |
| `RUN.CANCEL_REQUESTED` | Cancellation requested (triage decline, issue closed, etc.) |

### Eval

| Type | Description |
|------|-------------|
| `EVAL.DRIFT_DETECTED` | Eval system detected prompt/behavior drift |

## Dispatch Markers

Dispatch markers are the idempotency mechanism for the `decide()` function. The executor emits the marker *before* invoking the agent. On beacon re-delivery, `decide()` sees the marker in the event log and returns `Noop` instead of double-invoking.

For pre-PR steps (triage, architect), marker presence anywhere in the event log means "already dispatched." For post-PR steps (implementer revision, validators), the marker must appear *after* the triggering event -- successive `IMPL.ITERATION_REQUESTED` events each need their own implementer dispatch.

## UsagePayload Base

Every agent result event (`*.READY`) extends `UsagePayload`:

```python
class UsagePayload(Payload):
    token_in: int = 0
    token_out: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
```

The event_projector accumulates these onto the run's SUMMARY row (`total_token_in`, `total_token_out`, `total_cost_usd`, `total_duration_ms`).

## SQS Beacon Pattern

The wake-up mechanism:

1. `event_projector` writes an EVENT row to DDB (via transactional put)
2. DDB Stream emits an INSERT record for the new EVENT row
3. An EventBridge Pipe forwards the INSERT to the state-router's SQS queue (the "beacon")
4. `state_router` consumes the beacon, queries all `EVENT#*` rows for the run
5. `decide(events)` returns the next action
6. The executor applies the action (invoke agent, emit event, or noop)

There is no manual SQS beacon send -- the DDB Stream Pipe is the sole source of beacons.

## DynamoDB State Model

Single table (`runs`) with composite key `(pk, sk)`:

### Run rows

- `pk = RUN#{run_id}`, `sk = EVENT#{event_id}` -- one row per event, forming the timeline. Carries `type`, `envelope` (JSON), `run_id`, `project_slug`.
- `pk = RUN#{run_id}`, `sk = SUMMARY` -- one row per run. Carries `status` (latest event type), `updated_at`, `run_id`, `project_slug`, `requestor`, `target_repo`, `intent`, `pr_url`, token/cost/duration totals, GSI keys.

### GSIs

- Issue lookup: `gsi1pk = ISSUE#{source_issue_url}`, `gsi1sk = RUN#{run_id}`
- PR lookup: `gsi_pr = PR#{pr_url}` (for webhook handler to find the run from a PR URL)

### Idempotency

The EVENT row put uses `attribute_not_exists(sk)` as a condition. Re-delivered events fail the condition and the entire transaction rolls back -- no double-counted usage, no duplicate memory writes.

## Event Publishing

Events are published via `common.event_emit.publish()` which wraps the EventBridge `PutEvents` API. The `source` and `detail-type` fields are derived from the envelope's `actor_id` and `type`.
