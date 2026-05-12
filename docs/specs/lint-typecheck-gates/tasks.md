# Tasks — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-typecheck-gates`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Add quality_gate module with gate execution and result types
  - **Implements:** R-001, R-002, AC-001, AC-002, AC-003, AC-004, AC-005
  - **Touches:** `agents/implementer/src/implementer/quality_gate.py`, `agents/implementer/tests/test_quality_gate.py`
  - **Done when:** quality_gate.py exists with GateCommand, GateResult, GateOutcome dataclasses and run_gate() function that executes commands via subprocess with 60s timeout, truncates output to 4096 chars, and composes retry_prompt/blocked_reason. Unit tests in test_quality_gate.py pass. `uv run ruff check .` and `uv run ty check` pass.

- [ ] **T-002** — Add gate_commands module for resolving gate commands from stack profile
  - **Implements:** R-003, AC-006, AC-007
  - **Touches:** `agents/implementer/src/implementer/gate_commands.py`, `agents/implementer/tests/test_gate_commands.py`
  - **Done when:** gate_commands.py exists with resolve_gate_commands() that returns GateCommand list from a StackProfile (fetched from S3) or hardcoded ai-dlc defaults. Returns empty list when no profile/commands are discoverable. Unit tests in test_gate_commands.py pass. `uv run ruff check .` and `uv run ty check` pass.

- [ ] **T-003** — Integrate quality gate into implementer client execute flows
  - **Implements:** R-001, R-002, AC-001, AC-002, AC-003, AC-004, AC-005, AC-008
  - **Touches:** `agents/implementer/src/implementer/client.py`, `agents/implementer/src/implementer/options.py`, `agents/implementer/src/implementer/prompts.py`
  - **Depends on:** T-001, T-002
  - **Done when:** client.py's execute_initial and execute_iteration call the quality gate after drive_agent returns status='done'. On first failure, a constrained retry sub-session runs (max 8 turns, $1 budget). On second failure, blocked_reason is set. Gate is skipped when resolve_gate_commands returns empty. Existing unit tests still pass. `uv run ruff check .` and `uv run ty check` pass.
