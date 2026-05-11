# Tasks — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-gate`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Add implementer.lint_gate module with run_lint_gate using Makefile targets
  - **Implements:** AC-001, AC-004
  - **Touches:** `agents/implementer/src/implementer/lint_gate.py`, `agents/implementer/tests/test_lint_gate.py`
  - **Done when:** run_lint_gate(path) executes `make lint`, `make format`, `make type`, and `make test` via subprocess.run with a 60s timeout per command and cwd set to the repo root, returns a LintGateResult with per-command exit codes and truncated output (max 4096 chars per command), and unit tests cover pass, single-failure, all-failure, and timeout scenarios. `make lint` passes, `make format-check` passes, `make type` passes, `uv run pytest -q agents/implementer/tests/test_lint_gate.py` passes.

- [x] **T-002** — Integrate lint gate into execute_initial and execute_iteration with one-retry loop
  - **Implements:** AC-002, AC-003, AC-005, AC-006
  - **Touches:** `agents/implementer/src/implementer/client.py`, `packages/common/src/common/runtime.py`, `agents/implementer/tests/test_client.py`
  - **Depends on:** T-001
  - **Done when:** Both execute_initial and execute_iteration call run_lint_gate after the agent loop when the agent reported status='done' and made real changes; on failure the agent is resumed with lint error feedback for one retry; ImplementerResult carries a `lint_gate: LintGateResult | None` field (None when blocked); double-failure proceeds to commit with passed=False; unit tests verify the retry path, the pass-through path, and the blocked-skip path. `make lint` passes, `make format-check` passes, `make type` passes, `uv run pytest -q agents/implementer/tests/test_client.py` passes.
