# 03 — Cross-cutting feature

> **Slug:** `cross-cutting-feature`  ·  **Category:** breadth

## Intent

> Add structured-log JSON output to every route in the `echo` service. Use `structlog`, configure it once at startup, propagate a request id from `x-request-id` (generate one if absent), and add the request id to every log line for the request. Update `docs/MEMORY.md` to document the convention.

## Setup

`echo` has multiple FastAPI routes (from cases 01 + 02 + ad-hoc additions). No existing logging configuration.

## Expected behaviour

- Architect writes a 3-5 task spec `structured-logging`. The design proposes an ADR (`docs/ADRs/NNNN-structured-logging.md`) because logging conventions are a cross-cutting decision.
- Tasks: add structlog config + middleware, add a test that asserts request id propagation, update every route to use the configured logger, update `docs/MEMORY.md`.
- Implementer's first PR adds the ADR and the structlog config; subsequent PRs migrate routes one at a time.

## Pass criteria

- 3 ≤ task_count ≤ 6.
- One new ADR committed under `docs/ADRs/`.
- `MEMORY.md` "Decisions" section gains a bullet for the new ADR.
- Every route logs a `request_id` field; the request-id-propagation test passes.
- Total run cost < $4.
- No PR touches the project's overall test runner config (no scope creep).
