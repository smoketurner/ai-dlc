# Tasks — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-typecheck-gate`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Add LintGateConfig to packages/common
  - **Implements:** R-002, AC-005, AC-006
  - **Touches:** `packages/common/src/common/lint_gate.py`, `packages/common/tests/test_lint_gate.py`
  - **Done when:** packages/common/src/common/lint_gate.py exports LintGateConfig with a from_stack_profile classmethod that resolves lint and typecheck commands for Python/Astral (ruff check + ty check), Node (eslint), and unknown stacks (empty). Unit tests in packages/common/tests/test_lint_gate.py cover all branches. uv run ruff check and uv run ty check pass.

- [ ] **T-002** — Implement run_lint_gate and gate-fixer sub-session in the Implementer
  - **Implements:** R-001, R-003, AC-001, AC-002, AC-003, AC-004, AC-007
  - **Touches:** `agents/implementer/src/implementer/lint_gate.py`, `agents/implementer/src/implementer/prompts.py`, `agents/implementer/src/implementer/options.py`, `agents/implementer/tests/test_lint_gate.py`
  - **Depends on:** T-001
  - **Done when:** agents/implementer/src/implementer/lint_gate.py exports run_lint_gate that runs configured commands via subprocess, retries via a constrained Claude sub-session on failure, and returns LintGateResult. GATE_FIXER_SYSTEM_PROMPT is added to prompts.py. build_gate_fixer_options is added to options.py. Unit tests in agents/implementer/tests/test_lint_gate.py cover: all-pass, fail-then-fix, exhausted-retries, no-commands-skip, and timeout scenarios. uv run ruff check and uv run ty check pass.

- [ ] **T-003** — Wire the lint gate into execute_task and execute_iteration
  - **Implements:** R-001, AC-001, AC-003, AC-004
  - **Touches:** `agents/implementer/src/implementer/client.py`, `agents/implementer/tests/test_client_lint_gate.py`
  - **Depends on:** T-002
  - **Done when:** agents/implementer/src/implementer/client.py calls run_lint_gate after drive_agent returns status='done' and before finalize_task_branch. On gate failure, blocked_reason is set from the gate output. Existing tests in agents/implementer/tests/ still pass. A new integration-style test (mocked subprocess + mocked Claude SDK) in agents/implementer/tests/test_client_lint_gate.py verifies the end-to-end wiring. uv run ruff check and uv run ty check pass.
