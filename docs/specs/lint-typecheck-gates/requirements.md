# Requirements — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-typecheck-gates`

## Summary

Add deterministic, machine-enforced lint and typecheck gates that run after the Implementer agent finishes editing code but before the task advances to pr_open. When lint or typecheck fails, the implementer is given one automatic retry with the failure output as feedback; if the retry also fails, the task is blocked with a structured reason. This eliminates the expensive CI-failure → iteration loop for deterministic static-analysis violations.

## User stories

- **R-001** — As a platform operator, I want have lint and typecheck run deterministically after the implementer finishes code edits, before the task branch is pushed so that CI iteration loops caused by trivially-detectable static-analysis violations are eliminated.
- **R-002** — As a implementer agent, I want receive structured lint/typecheck failure output as feedback when my edits fail the gate so that I can fix the violations in a tight local loop without a full iteration cycle through the state machine.
- **R-003** — As a platform operator, I want configure which gate commands run per target repo via the stack profile so that the gate adapts to each repo's toolchain without manual per-repo configuration.

## Acceptance criteria

- **AC-001** (R-001) — WHEN the implementer agent calls finish with status='done', THE SYSTEM SHALL run the configured lint command(s) against the working tree before committing and pushing the task branch.
- **AC-002** (R-001) — WHEN the implementer agent calls finish with status='done', THE SYSTEM SHALL run the configured typecheck command against the working tree before committing and pushing the task branch.
- **AC-003** (R-001) — IF lint or typecheck exits non-zero on the first attempt, THEN THE SYSTEM SHALL feed the failure stdout+stderr (truncated to 4096 chars) back to the agent as a retry prompt and re-run the agent for one additional turn.
- **AC-004** (R-001) — IF lint or typecheck exits non-zero after the retry turn, THEN THE SYSTEM SHALL set blocked_reason to a structured message containing the failing command and its truncated output, and emit TASK.BLOCKED instead of TASK.READY.
- **AC-005** (R-002) — WHEN the gate retry prompt is composed, THE SYSTEM SHALL include the exact command that failed, its exit code, and the last 4096 bytes of combined stdout+stderr in the retry message.
- **AC-006** (R-003) — WHEN the implementer session starts and the target repo has a StackProfile with lint_command or format_command on the root component, THE SYSTEM SHALL use those commands as the gate's lint step.
- **AC-007** (R-003) — IF no lint_command, format_command, or typecheck command is discoverable from the stack profile or MEMORY.md, THEN THE SYSTEM SHALL skip the gate entirely and proceed to commit+push as before.
- **AC-008** (R-001) — WHEN the implementer is in iteration mode (iteration_count > 0) and calls finish with status='done', THE SYSTEM SHALL run the same lint/typecheck gate before committing and pushing the iteration's changes.

## Out of scope

- Running the full test suite as a gate (tests are non-deterministic and slow; CI handles them)
- Adding gates to the Architect or Critic agent steps (spec documents are Markdown, not code)
- Modifying the CI workflow itself
- Adding new state machine states — the gate runs inside the implementer container, not as a separate orchestration step

## Open questions

- Should the gate also run `ruff format --check` as a separate step or rely on `ruff check` covering format violations via lint rules? Conservative default: run both lint and format-check as separate commands since the project Makefile does.
