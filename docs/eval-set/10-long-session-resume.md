# 10 — Long-session resume

> **Slug:** `long-session-resume`  ·  **Category:** robustness

## Intent

> Migrate the `echo` service from FastAPI's deprecated `on_event("startup")` decorator to the lifespan context-manager pattern. Touch every place that registers a startup or shutdown callback.

## Setup

`echo` repo with at least 4 `on_event` registrations across 3 files. Roadmap-relevant property: the spec's tasks list will be long enough that a human reviewer takes more than 24 hours to approve the spec gate (the platform's heartbeat window).

## Expected behaviour

- Architect produces a spec with 5+ tasks.
- The spec gate stays in `WaitForSpecApproval` for 25-30 hours. Step Functions heartbeats keep the wait open; the AgentCore session ages out and gets re-bootstrapped from S3-snapshotted MEMORY.md when the implementer is finally invoked.
- Once approved, the implementer chews through tasks one PR at a time. Each task's session resumes the prior memory rather than starting cold.

## Pass criteria

- The spec gate's `WaitForSpecApproval` state survives the heartbeat window without a Step Functions timeout.
- The first implementer task's prompt log shows it loaded `docs/MEMORY.md` from `/workspace/spec` (not an empty filesystem).
- The final repo has zero `on_event(` occurrences.
- All existing tests pass; the lifespan migration test that the spec required is present.
- Total run cost < $6 (longer because of the multi-task implementer chain).
