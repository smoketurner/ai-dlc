# Tasks — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-typecheck-gates`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Add lint_gate module with subprocess runner and LintGateResult
  - **Implements:** R-001, R-004
  - **Touches:** `agents/implementer/src/implementer/lint_gate.py`, `agents/implementer/tests/test_lint_gate.py`
  - **Done when:** agents/implementer/src/implementer/lint_gate.py exists with run_lint_gate() function that executes ruff check and ty check via subprocess, returns LintGateResult, handles command-not-found gracefully, truncates output to 4096 chars, and logs structured gate results. Unit tests in agents/implementer/tests/test_lint_gate.py cover AC-001, AC-002, AC-003, AC-007, AC-008, AC-009 with mocked subprocess. All tests pass, ruff check and ty check pass on the new code.

- [ ] **T-002** — Integrate lint gate into implementer client with retry loop and lint-fix prompt
  - **Implements:** R-001, R-002, R-003, R-004
  - **Touches:** `agents/implementer/src/implementer/client.py`, `agents/implementer/src/implementer/prompts.py`, `agents/implementer/tests/test_lint_gate.py`
  - **Depends on:** T-001
  - **Done when:** execute_initial and execute_iteration in client.py call run_lint_gate() after drive_agent and before finalize_task_branch/merge. On failure, re-invoke drive_agent with LINT_FIX_PROMPT_TEMPLATE (added to prompts.py) up to MAX_LINT_RETRIES=2 times. On exhaustion, set blocked_reason. Tests in agents/implementer/tests/test_client.py (or test_lint_gate.py) cover AC-004, AC-005, AC-006 with mocked drive_agent + run_lint_gate. All tests pass, ruff check and ty check pass on the new code.
