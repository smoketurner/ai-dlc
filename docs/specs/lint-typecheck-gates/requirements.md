# Requirements — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-typecheck-gates`

## Summary

Add a deterministic, platform-enforced lint and typecheck gate inside the Implementer agent's flow. After the Claude Agent SDK session finishes editing code and before TASK.READY is emitted, the platform runs ruff check and ty check against the workspace. If either fails, the agent is re-invoked with the structured error output as feedback for a bounded number of retries. Only when both pass (or the retry budget is exhausted, surfacing TASK.BLOCKED) does the flow proceed.

## User stories

- **R-001** — As a platform operator, I want have lint and typecheck run deterministically after every implementer session, blocking TASK.READY emission on failure so that no task PR is opened with known lint or type errors that a subprocess can catch.
- **R-002** — As a platform operator, I want have the implementer automatically retry with structured lint/type error output when the gate fails so that most lint/type issues are fixed without a full CI round-trip or human intervention.
- **R-003** — As a platform operator, I want have a bounded retry budget for the lint/type gate so the system does not loop indefinitely so that the task surfaces as TASK.BLOCKED with the remaining errors after the budget is exhausted.
- **R-004** — As a platform operator, I want see lint and typecheck results in the FinishReport and audit trail so that I can diagnose why a task was blocked or how many gate retries it took.

## Acceptance criteria

- **AC-001** (R-001) — WHEN the implementer agent session completes with status='done' and has uncommitted or just-committed changes, THE SYSTEM SHALL run `ruff check` and `ty check` against the repo working tree before emitting TASK.READY or proceeding to the merge step.
- **AC-002** (R-001) — WHEN ruff check exits with a non-zero return code, THE SYSTEM SHALL suppress TASK.READY emission and capture the full stdout/stderr as structured lint failure output.
- **AC-003** (R-001) — WHEN ty check exits with a non-zero return code, THE SYSTEM SHALL suppress TASK.READY emission and capture the full stdout/stderr as structured typecheck failure output.
- **AC-004** (R-002) — WHEN the lint/type gate fails and the gate retry count is below MAX_LINT_RETRIES, THE SYSTEM SHALL re-invoke the Claude Agent SDK session with a user prompt containing the lint/type errors and increment the gate retry counter.
- **AC-005** (R-002) — THE SYSTEM SHALL pass the exact ruff/ty stderr output (truncated to 4096 chars) as the retry prompt so the agent sees concrete file:line:col diagnostics.
- **AC-006** (R-003) — IF the gate retry count reaches MAX_LINT_RETRIES and lint/type still fails, THEN THE SYSTEM SHALL set blocked_reason to a message containing the remaining lint/type errors and emit TASK.BLOCKED instead of TASK.READY.
- **AC-007** (R-001) — WHEN both ruff check and ty check exit with return code 0, THE SYSTEM SHALL proceed to the merge step and ultimately emit TASK.READY as before.
- **AC-008** (R-004) — WHEN the lint/type gate completes (pass or fail), THE SYSTEM SHALL log a structured event with gate_pass (bool), retry_count (int), ruff_exit_code, ty_exit_code, and truncated diagnostic output.
- **AC-009** (R-001) — WHILE the target repo does not have ruff or ty installed (uv run ruff/ty returns command-not-found), THE SYSTEM SHALL skip the corresponding check and log a warning rather than blocking the task.

## Out of scope

- Running pytest or other test suites as part of this gate (tests are covered by CI)
- Applying ruff --fix automatically (the agent should fix intentionally)
- Adding new CI workflow steps (the gate is inside the implementer container)
- Changing the state machine or adding new TaskState values
- Running lint/type gates for non-Python repos (future work)

## Open questions

- Should the gate also run `ruff format --check` and block on formatting violations, or leave formatting to the agent's discretion? Conservative default: include format check since the project convention requires it.
- Should MAX_LINT_RETRIES be 1 or 2? Conservative default: 2 (total of 3 attempts including the initial agent run).
