# Requirements — Deterministic lint/typecheck gate between agent steps

> **Spec slug:** `lint-gate`

## Summary

Add a deterministic (non-LLM) lint and typecheck gate that runs between the implementer's task completion and the LLM-based validation pass. The gate executes `ruff check`, `ruff format --check`, and `ty check` against the integrated impl branch in a Code Interpreter sandbox. On failure, the implementer is dispatched in revision mode to fix lint/type errors before validators fire.

## User stories

- **R-001** — As a platform operator, I want have the state machine enforce lint and typecheck cleanliness on the impl branch before LLM validators fire so that broken code never reaches the reviewer/tester/code-critic agents, saving token spend on obviously-fixable issues.
- **R-002** — As a platform operator, I want see lint gate pass/fail status on the run's DynamoDB row and in EventBridge events so that I can monitor gate outcomes and debug failures via the same observability stack as other state transitions.
- **R-003** — As a implementer agent (downstream consumer), I want receive structured lint/type error output as revision feedback when the gate fails so that I can fix the exact errors without guessing what went wrong.

## Acceptance criteria

- **AC-001** (R-001) — WHEN the run reaches `tasks_complete` and the state_router dispatches the next step, THE SYSTEM SHALL invoke the lint_gate Lambda (advancing the run to `lint_gate_running`) before any LLM validator is dispatched.
- **AC-002** (R-001) — WHEN the lint_gate Lambda completes with all checks passing (exit code 0 for each), THE SYSTEM SHALL emit a `LINT_GATE.PASSED` event on the platform EventBridge bus carrying the impl PR head SHA and the list of commands that ran.
- **AC-003** (R-001) — WHEN the lint_gate Lambda completes with any check failing (non-zero exit code), THE SYSTEM SHALL emit a `LINT_GATE.FAILED` event on the platform EventBridge bus carrying the failing command, its stderr output (tail 4 KiB), and the impl PR head SHA.
- **AC-004** (R-001) — WHEN the event_projector receives `LINT_GATE.PASSED` while the run is in `lint_gate_running`, THE SYSTEM SHALL advance the run state from `lint_gate_running` to `validation_running`.
- **AC-005** (R-001) — WHEN the event_projector receives `LINT_GATE.FAILED` while the run is in `lint_gate_running`, THE SYSTEM SHALL advance the run state from `lint_gate_running` to `revising` with the lint errors stored as `pending_revision_feedback` on the STATE row.
- **AC-006** (R-001) — THE SYSTEM SHALL run exactly three commands in order inside the sandbox: `ruff check .`, `ruff format --check .`, `ty check`; stop at the first non-zero exit.
- **AC-007** (R-001) — IF the lint_gate Lambda fails to start a Code Interpreter session or the sandbox extract step fails, THEN THE SYSTEM SHALL emit `LINT_GATE.FAILED` with `error_class="infrastructure"` so the circuit breaker retries on the next beacon.
- **AC-008** (R-002) — WHEN `LINT_GATE.PASSED` or `LINT_GATE.FAILED` is projected by the event_projector, THE SYSTEM SHALL write `lint_gate_result`, `lint_gate_sha`, and `lint_gate_at` (ISO timestamp) onto the run's STATE row.
- **AC-009** (R-003) — WHEN the state_router dispatches the implementer in revision mode after a lint gate failure, THE SYSTEM SHALL include the lint gate's stderr output and failing command in the `pending_revision_feedback` list so the implementer receives the exact errors.

## Out of scope

- Per-task lint gates (running lint after each individual task before it merges into the impl branch)
- Customizable lint commands per target repo (always uses the workspace-root ruff + ty)
- Dashboard UI for lint gate results (observability via DDB + EventBridge is sufficient for now)
- Running pytest as part of the lint gate (tests are the tester agent's domain)

## Open questions

- Should the lint gate also run `pip-audit` (the CI `audit` step)? Defaulting to no — audit is slow and catches supply-chain issues, not code quality.
- Should infrastructure failures (sandbox crash) count toward the circuit breaker's `dispatch_failure_count`? Defaulting to yes — same pattern as agent dispatch failures.
