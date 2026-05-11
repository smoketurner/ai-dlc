# Requirements — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-gate`

## Summary

Add deterministic lint, format, type-check, and test gates inside the Implementer agent's execution flow so that `make lint`, `make format`, `make type`, and `make test` run automatically after the agent finishes editing — before the PR is committed and pushed. If the gate fails, the agent receives the error output as structured feedback and gets one in-process retry to fix the issues, eliminating the slow CI→webhook→iteration round-trip for trivially fixable errors.

## User stories

- **R-001** — As a platform operator, I want lint, format, type-check, and test errors caught inside the Implementer session so that PRs never land with trivially fixable lint/type/test violations.
- **R-002** — As a platform operator, I want the Implementer to self-correct gate failures within the same session so that CI iteration cycles are not wasted on deterministic errors.
- **R-003** — As a platform operator, I want visibility into whether the lint gate passed or required a retry so that I can monitor agent quality over time.

## Acceptance criteria

- **AC-001** (R-001) — WHEN the Implementer agent calls finish with status='done' and has made real changes, THE SYSTEM SHALL run `make lint`, `make format`, `make type`, and `make test` sequentially against the repo working tree from the repo root before committing.
- **AC-002** (R-002) — WHEN the lint gate reports one or more command failures (non-zero exit code), THE SYSTEM SHALL feed the combined stdout/stderr of all failed commands back to the Claude Agent SDK session as a follow-up user message and resume the agent for one additional pass.
- **AC-003** (R-002) — IF the lint gate fails on the retry pass (second consecutive failure), THEN THE SYSTEM SHALL proceed with commit and push (allowing CI to catch the residual) and record the failure in the ImplementerResult lint_gate field with passed=False.
- **AC-004** (R-001) — THE SYSTEM SHALL run the lint gate commands via `make` (not direct `uv run` invocations) so that the Makefile remains the single source of truth for tool versions and flags.
- **AC-005** (R-003) — WHEN the lint gate completes (pass or fail), THE SYSTEM SHALL include a `lint_gate` field in the ImplementerResult carrying pass/fail status, retry count, and truncated error output per command.
- **AC-006** (R-001) — WHILE the Implementer is running an iteration (iteration_count > 0), THE SYSTEM SHALL apply the same lint gate before pushing the fix commit.

## Out of scope

- Modifying the CI workflow itself
- Adding lint gates to non-Implementer agents (Architect, Critic, etc.)
- Running integration or live-AWS tests as part of the gate
- Changing the Makefile targets themselves
