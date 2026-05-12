# Design — Deterministic lint/typecheck gate between agent steps

> **Spec slug:** `lint-gate`

## Approach

Insert a new deterministic Lambda (`lambdas/lint_gate/`) into the run-level state machine between `tasks_complete` and `validation_running`. The Lambda uses the existing Code Interpreter sandbox infrastructure (same as reviewer/tester) to clone the impl branch and run `ruff check .`, `ruff format --check .`, `ty check` in sequence. It emits a typed EventBridge event (`LINT_GATE.PASSED` or `LINT_GATE.FAILED`); the event_projector advances state accordingly. On failure, the run transitions to `revising` (reusing the existing revision loop) with the lint errors as feedback; on pass, it transitions to `validation_running` where the LLM validators fire.

The lint gate Lambda is invoked by the state_router the same way agents are (fire-and-forget via `InvokeAgent` action with a state advance guard), but it is not an LLM agent — it's a short deterministic function that runs three shell commands and emits one event. This keeps the state machine's dispatch/project split clean.

Key refactor: `handle_tasks_complete` no longer dispatches validators directly. Instead it dispatches the lint gate. A new `handle_validation_running` handler (replacing the current `noop_waiting` mapping) dispatches the validators. `REVISION.READY` now advances to `tasks_complete` (not `validation_running`) so every revision re-runs the lint gate before validators fire.

## Components

- **lint_gate Lambda handler** (`lambdas/lint_gate/src/lint_gate/handler.py`) — Deterministic Lambda that clones the impl branch into a Code Interpreter sandbox, runs ruff check + ruff format --check + ty check, and emits LINT_GATE.PASSED or LINT_GATE.FAILED on EventBridge
- **LintGatePassed / LintGateFailed payloads** (`packages/common/src/common/events.py`) — Typed Pydantic payloads for the LINT_GATE.PASSED and LINT_GATE.FAILED events
- **RunState.lint_gate_running** (`packages/common/src/common/state.py`) — New state in the run-level state machine between tasks_complete and validation_running
- **LINT_GATE transitions** (`packages/common/src/common/state_transitions.py`) — Map (LINT_GATE.PASSED, lint_gate_running) → validation_running and (LINT_GATE.FAILED, lint_gate_running) → revising; change (REVISION.READY, revising) → tasks_complete
- **handle_tasks_complete + handle_validation_running** (`lambdas/state_router/src/state_router/dispatch_run.py`) — Split: tasks_complete dispatches lint gate; new validation_running handler dispatches reviewer/tester/code_critic
- **lint_gate_function_name()** (`lambdas/state_router/src/state_router/config.py`) — Env-var accessor for AIDLC_LINT_GATE_FUNCTION_NAME
- **LINT_GATE event projection** (`lambdas/event_projector/src/event_projector/handler.py`) — Advance state on LINT_GATE events; write lint_gate_result/sha/at attrs; on failure store stderr as pending_revision_feedback

## Data model

```text
**DynamoDB STATE row additions:**
- `lint_gate_result`: String — `"passed"` or `"failed"`
- `lint_gate_sha`: String — impl branch head SHA at gate run time
- `lint_gate_at`: String — ISO 8601 timestamp of gate completion
- On LINT_GATE.FAILED: projector appends to `pending_revision_feedback` list with `{"source": "lint_gate", "body": "<stderr tail>", "command": "<failed_command>"}`

**EventBridge payloads:**
```
LintGatePassed:
  project_slug, spec_slug, pr_url, head_sha: str
  commands_run: list[str]
  duration_ms: int
  session_id: str

LintGateFailed:
  project_slug, spec_slug, pr_url, head_sha: str
  failed_command: str
  stderr: str (tail 4 KiB)
  error_class: "lint" | "format" | "typecheck" | "infrastructure"
  duration_ms: int
  session_id: str
```

**New EventType literals:** `"LINT_GATE.PASSED"`, `"LINT_GATE.FAILED"`
**New RunState:** `lint_gate_running`
```

## Sequence

```text
1. All tasks reach done states → run advances to `tasks_complete`
2. `handle_tasks_complete` dispatches lint_gate Lambda via `InvokeRepoHelper` (synchronous) + advances to `lint_gate_running`
3. lint_gate Lambda: starts Code Interpreter session, downloads impl branch tarball via `repo_helper.get_pr_archive_url`, extracts into sandbox
4. Runs `ruff check .` → on non-zero exit → emits `LINT_GATE.FAILED(error_class="lint")` → return
5. Runs `ruff format --check .` → on non-zero exit → emits `LINT_GATE.FAILED(error_class="format")` → return
6. Runs `ty check` → on non-zero exit → emits `LINT_GATE.FAILED(error_class="typecheck")` → return
7. All pass → emits `LINT_GATE.PASSED`
8a. PASSED: projector advances `lint_gate_running → validation_running`; next beacon fires `handle_validation_running` which dispatches reviewer + tester + code_critic
8b. FAILED: projector advances `lint_gate_running → revising` + stores feedback; next beacon fires implementer in revision mode
9. After revision: `REVISION.READY` → projector advances `revising → tasks_complete` → lint gate re-runs (loop until clean)
```

## Testing strategy

**Unit tests (lambdas/lint_gate/tests/):**
- AC-002/AC-006: mock CI sandbox returning exit_code=0 for all three commands; assert LINT_GATE.PASSED emitted with correct payload.
- AC-003/AC-006: mock ruff check returning exit_code=1; assert LINT_GATE.FAILED with error_class="lint" and stderr.
- AC-007: mock CI session start raising; assert LINT_GATE.FAILED with error_class="infrastructure".
- Stop-on-first: mock ruff check failing; assert format/ty never called.

**Unit tests (lambdas/state_router/tests/):**
- AC-001: assert handle_tasks_complete returns lint_gate dispatch action.
- AC-004: assert handle_validation_running dispatches validators.
- New RUN_DISPATCH entry for lint_gate_running is noop_waiting.

**Unit tests (packages/common/tests/):**
- AC-004: apply_run_transition(LINT_GATE.PASSED, lint_gate_running) == validation_running.
- AC-005: apply_run_transition(LINT_GATE.FAILED, lint_gate_running) == revising.
- REVISION.READY from revising now → tasks_complete.
- Pydantic model validation for LintGatePassed/LintGateFailed.

All tests use pytest + moto fixtures from existing conftest.py.

## Failure modes & mitigations

- Code Interpreter session fails to start → LINT_GATE.FAILED with error_class="infrastructure"; circuit breaker retries on next beacon.
- Sandbox extract fails (tarball URL expired) → same infrastructure failure path.
- lint_gate Lambda crashes (OOM/timeout) → state_router's rollback-after-failure reverts tasks_complete → lint_gate_running advance and retries.
- Target repo doesn't use ruff/ty → commands fail; gate emits FAILED. Acceptable: ai-dlc is the only target repo today.

## Trade-offs

- Adding `lint_gate_running` state increases state machine surface by one entry — acceptable given it prevents wasted LLM token spend on lint-fixable code.
- Changing REVISION.READY → tasks_complete (from validation_running) means reviewer-triggered revisions also re-run the lint gate — desirable (ensures revisions stay clean) but adds ~30s latency.
- Code Interpreter cold-start adds latency for a 3-command check — acceptable; future optimization could run lint inside the Lambda directly for small repos.

## References

- packages/common/src/common/sandbox.py — existing Code Interpreter sandbox pattern
- lambdas/state_router/src/state_router/dispatch_run.py — current tasks_complete and validation dispatch
- packages/common/src/common/state.py — RunState enum
- packages/common/src/common/state_transitions.py — RUN_TRANSITIONS mapping
- Makefile — `make ci` target showing the lint/format/type/test chain
