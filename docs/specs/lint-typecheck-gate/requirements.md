# Requirements — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-typecheck-gate`

## Summary

Add a platform-enforced lint and typecheck gate that runs deterministically after the Implementer agent commits code but before the task advances to pr_open. When the gate fails, the Implementer automatically iterates to fix the violations without human intervention or waiting for GitHub Actions CI. This eliminates the slow feedback loop where CI fails on the PR and a TASK.ITERATION_REQUESTED event fires minutes later.

## User stories

- **R-001** — As a platform operator, I want have lint and typecheck violations caught deterministically before a task PR becomes visible to reviewers so that reviewers never see PRs with trivially-fixable lint/type errors, reducing review noise.
- **R-002** — As a platform operator, I want configure which lint/typecheck commands run in the gate per target repo via the stack profile so that the gate works for any repo the platform manages, not just ai-dlc itself.
- **R-003** — As a Implementer agent, I want receive structured feedback from a failed lint/typecheck gate so I can fix violations in the same session without a full iteration cycle so that violations are fixed in-session (sub-second feedback) rather than waiting for a new dispatch cycle.

## Acceptance criteria

- **AC-001** (R-001) — WHEN the Implementer agent calls finish with status='done' and has committed changes to the task branch, THE SYSTEM SHALL run the configured lint and typecheck commands against the task branch working tree before emitting TASK.READY.
- **AC-002** (R-001) — IF any lint or typecheck command exits with a non-zero code, THEN THE SYSTEM SHALL suppress the TASK.READY event and feed the command's stderr/stdout back to the Implementer agent as in-session retry context, up to a configurable max-retries cap.
- **AC-003** (R-001) — WHEN all lint and typecheck commands exit zero after the Implementer's edits (initial or retry), THE SYSTEM SHALL emit TASK.READY and allow the task to advance to pr_open normally.
- **AC-004** (R-001) — IF the Implementer exhausts the max-retries cap for the lint/typecheck gate, THEN THE SYSTEM SHALL emit TASK.BLOCKED with a blocked_reason containing the last gate failure output.
- **AC-005** (R-002) — WHEN the Implementer session starts and the target repo's stack profile includes lint_command or a ty/mypy type-check command, THE SYSTEM SHALL resolve the gate commands from the stack profile's root-level lint_command and type-check conventions (ruff check for lint, ty check for typecheck when the project uses the Astral toolchain).
- **AC-006** (R-002) — IF the target repo's stack profile has no detectable lint or typecheck command, THEN THE SYSTEM SHALL skip the gate entirely and emit TASK.READY immediately after the agent finishes.
- **AC-007** (R-003) — WHEN a gate command fails, THE SYSTEM SHALL present the failing command name, exit code, and the last 4096 bytes of combined stdout+stderr to the Implementer agent as a structured retry prompt within the same Claude Agent SDK session.

## Out of scope

- Running the full test suite in the gate (tests remain in CI)
- Enforcing format-check in the gate (the Implementer already runs ruff format via its system prompt; format is not a correctness gate)
- Adding new state machine states — the gate runs within the existing implementer_running phase
- Modifying the GitHub Actions CI workflow

## Open questions

- Should the gate also run ruff format --check (format verification) or only ruff check (lint)? Assumed: lint + typecheck only, since format is auto-fixable and the agent already formats.
- What is the right max-retries cap? Assumed: 3 retries (4 total attempts including the initial). This is configurable via an env var.
