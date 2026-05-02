# 09 — HITL reject loop

> **Slug:** `hitl-reject-loop`  ·  **Category:** robustness

## Intent

> Add a `GET /env` route to the `echo` service that returns the names (not values) of every `AIDLC_*` env var present.

## Setup

`echo` repo. The reviewer will deliberately reject the first SPEC.READY with feedback: "Don't list env var names — that's an information leak. Return a constant `{"env": "running"}` instead."

## Expected behaviour

- The Architect produces an initial spec matching the user's literal request.
- The reviewer rejects via `/aidlc reject ...` with the feedback above.
- The Architect is re-invoked with `prior_feedback` populated; the new spec drops the env-listing approach and switches to the constant.
- The Implementer never opens a PR for the rejected design.

## Pass criteria

- Exactly one rejection round on the spec gate.
- The retried spec's `requirements.md` summary differs structurally from the first attempt.
- AgentCore Memory's EPISODIC strategy records both attempts (visible via `aws bedrock-agentcore-data list-events --memory-id ... --actor-id <actor> --session-id <run>`).
- Total run cost < $4 (two architect invocations + one final implementer pass).
