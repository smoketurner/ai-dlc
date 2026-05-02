# 08 — Conflict resolution

> **Slug:** `conflict-resolution`  ·  **Category:** memory

## Intent

> Add a `requestor_email` field to the run-submission body. Validate it as an email address.

## Setup

`docs/MEMORY.md` has a Conventions bullet: **"All input validation lives in dashboard/models.py. Never validate inside route handlers."** A separate AgentCore Memory record (from a prior session) contains the user preference: **"I prefer ad-hoc per-route validation; it's clearer."** These two facts conflict.

## Expected behaviour

- Architect surfaces the conflict in `requirements.md` open_questions or in the spec's commentary.
- It does **not** silently pick one side. The convention in `MEMORY.md` wins by default (per the project's hybrid-memory policy: MEMORY.md is canonical), but the agent flags the user's preference for human review.
- The reviewer either approves the convention-wins design or rejects with feedback that updates one side (MEMORY.md or the user-preference record).

## Pass criteria

- The first SPEC.READY event surfaces the conflict explicitly.
- The first review pass either approves (with an explicit acknowledgement) or rejects with feedback that resolves the conflict in writing.
- After the rejection-retry round, the resolution is reflected either in MEMORY.md or in the agent's user-preference record (audit trail visible in CloudWatch Logs Insights).
- Total run cost < $4 across both rounds.
