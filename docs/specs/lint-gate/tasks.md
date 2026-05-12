# Tasks — Deterministic lint/typecheck gate between agent steps

> **Spec slug:** `lint-gate`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Add LINT_GATE event types, payloads, and RunState.lint_gate_running
  - **Implements:** R-001, R-002
  - **Touches:** `packages/common/src/common/state.py`, `packages/common/src/common/state_transitions.py`, `packages/common/src/common/events.py`, `packages/common/tests/test_state_transitions.py`
  - **Done when:** RunState.lint_gate_running exists in state.py; EventType includes LINT_GATE.PASSED and LINT_GATE.FAILED; LintGatePassed and LintGateFailed Pydantic models exist in events.py with correct fields; RUN_TRANSITIONS maps (LINT_GATE.PASSED, lint_gate_running) → validation_running and (LINT_GATE.FAILED, lint_gate_running) → revising; (REVISION.READY, revising) now maps to tasks_complete; unit tests for all new/changed transitions pass; ruff check + ruff format --check + ty check pass on the diff.

- [ ] **T-002** — Implement lint_gate Lambda handler
  - **Implements:** R-001, R-002, R-003
  - **Touches:** `lambdas/lint_gate/pyproject.toml`, `lambdas/lint_gate/src/lint_gate/__init__.py`, `lambdas/lint_gate/src/lint_gate/handler.py`, `lambdas/lint_gate/tests/__init__.py`, `lambdas/lint_gate/tests/test_handler.py`
  - **Depends on:** T-001
  - **Done when:** lambdas/lint_gate/ package exists with pyproject.toml, __init__.py, and handler.py; handler clones impl branch via sandbox helpers, runs ruff check / ruff format --check / ty check in sequence (stop on first failure), emits LINT_GATE.PASSED or LINT_GATE.FAILED via common.event_emit.publish; unit tests mock Code Interpreter and assert correct event emission for pass, lint fail, format fail, typecheck fail, and infrastructure failure; ruff + ty pass.

- [ ] **T-003** — Wire lint gate into state_router dispatch and event_projector
  - **Implements:** R-001, R-002, R-003
  - **Touches:** `lambdas/state_router/src/state_router/dispatch_run.py`, `lambdas/state_router/src/state_router/config.py`, `lambdas/event_projector/src/event_projector/handler.py`, `lambdas/state_router/tests/test_dispatch_run.py`
  - **Depends on:** T-001, T-002
  - **Done when:** handle_tasks_complete dispatches lint_gate Lambda (via InvokeRepoHelper or direct Lambda invoke) + advances to lint_gate_running; new handle_validation_running handler dispatches reviewer + tester + code_critic (logic moved from old handle_tasks_complete); RUN_DISPATCH maps lint_gate_running → noop_waiting and validation_running → handle_validation_running; config.py exposes lint_gate_function_name(); event_projector projects LINT_GATE.PASSED (advance + write attrs) and LINT_GATE.FAILED (advance to revising + store feedback); existing state_router tests updated; ruff + ty pass.
