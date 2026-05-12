# Design — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-typecheck-gates`

## Approach

Add a `lint_gate` module to the implementer agent that runs deterministic subprocess checks (ruff check, ty check) after the agent session completes. The gate is called from `execute_initial` and `execute_iteration` in `client.py` between the `drive_agent` call and the `finalize_task_branch` / merge step. On failure, the gate returns structured diagnostics; the caller re-invokes `drive_agent` with a lint-fix prompt up to MAX_LINT_RETRIES times. The gate is a pure function of the working tree — no network, no DDB, no state machine changes. The retry loop lives entirely inside the implementer's existing async flow, invisible to the state router.

## Components

- **LintGate** (`agents/implementer/src/implementer/lint_gate.py`) — Runs ruff check and ty check as subprocesses against the repo working tree; returns a structured LintGateResult (pass/fail, exit codes, truncated diagnostics)
- **LintGateResult** (`agents/implementer/src/implementer/lint_gate.py`) — Frozen dataclass holding gate_pass, ruff_exit_code, ty_exit_code, ruff_output, ty_output, and a combined error summary for the retry prompt
- **execute_initial / execute_iteration updates** (`agents/implementer/src/implementer/client.py`) — Wrap the post-agent flow in a retry loop that calls run_lint_gate() and re-invokes drive_agent with a lint-fix prompt on failure
- **LINT_FIX_PROMPT_TEMPLATE** (`agents/implementer/src/implementer/prompts.py`) — User-prompt template for the lint-fix retry session, containing the exact ruff/ty diagnostics and instructions to fix only the reported issues
- **lint_gate unit tests** (`agents/implementer/tests/test_lint_gate.py`) — Test the gate subprocess wrapper with mocked subprocess calls, verifying pass/fail/skip behaviour and output truncation

## Data model

```text
```
@dataclass(frozen=True, slots=True)
class LintGateResult:
    gate_pass: bool
    ruff_exit_code: int | None  # None when skipped (not installed)
    ty_exit_code: int | None    # None when skipped
    ruff_output: str            # truncated to 4096 chars
    ty_output: str              # truncated to 4096 chars

    @property
    def error_summary(self) -> str:
        """Combined diagnostic output for the retry prompt."""
        ...
```

No new DDB attributes, no new events, no schema changes. The gate is purely internal to the implementer container.
```

## Sequence

```text
1. `execute_initial` / `execute_iteration` calls `drive_agent(user_prompt)` → agent edits code.
2. If `compute_blocked_reason` returns non-None → skip gate (already blocked).
3. Call `run_lint_gate()` → subprocess `uv run ruff check .` + `uv run ty check .` in repo_path().
4. If `LintGateResult.gate_pass` → proceed to `finalize_task_branch` + merge.
5. If not pass and `lint_retry_count < MAX_LINT_RETRIES`:
   a. Increment `lint_retry_count`.
   b. Compose a lint-fix prompt from `LINT_FIX_PROMPT_TEMPLATE` + `result.error_summary`.
   c. Call `drive_agent(lint_fix_prompt)` again (same session, same branch).
   d. Go to step 3.
6. If not pass and retries exhausted → set `blocked_reason` to the remaining errors.
7. `finalize_task_branch` / `finalize_iteration_branch` proceeds as before.
```

## Testing strategy

Unit tests in `agents/implementer/tests/test_lint_gate.py`:
- AC-001/AC-007: mock subprocess.run to return exit 0 for both ruff and ty; assert `gate_pass=True`.
- AC-002: mock ruff returning exit 1 with diagnostic output; assert `gate_pass=False`, `ruff_output` populated.
- AC-003: mock ty returning exit 1; assert `gate_pass=False`, `ty_output` populated.
- AC-005: verify `error_summary` includes file:line:col diagnostics truncated at 4096 chars.
- AC-009: mock ruff returning FileNotFoundError (command not found); assert `ruff_exit_code=None`, gate still runs ty, logs warning.
- AC-006: integration-style test in `test_client.py` mocking `drive_agent` + `run_lint_gate` to fail MAX_LINT_RETRIES+1 times; assert `blocked_reason` contains lint errors.
- AC-004: same setup but gate passes on retry 2; assert `drive_agent` called twice total, no blocked_reason.
- AC-008: assert structlog output contains the expected gate fields (gate_pass, retry_count, exit codes).

All tests use `unittest.mock.patch` on `subprocess.run` — no real ruff/ty execution needed.

## Failure modes & mitigations

- ruff or ty binary not available in the container image → gate skips that check (AC-009), logs warning. The base implementer image includes uv + the workspace deps, so ruff/ty are available for ai-dlc itself. For target repos that don't use ruff/ty, the gate degrades gracefully.
- Subprocess hangs (infinite loop in a ruff plugin) → mitigated by subprocess timeout (30s per tool). On timeout, treat as failure and include timeout message in diagnostics.
- Agent introduces new lint errors while fixing old ones (whack-a-mole) → bounded by MAX_LINT_RETRIES. After 2 retries the task surfaces as blocked with the remaining errors for human review.

## Trade-offs

- Running lint/type inside the implementer container adds ~5-15s per gate invocation (two subprocess calls). Acceptable given the alternative is a full CI round-trip (2-5 minutes).
- Re-invoking the agent on lint failure consumes additional tokens (~$0.10-0.50 per retry). Bounded by MAX_LINT_RETRIES=2 so worst case is 3x the base cost for a single task.
- The gate only covers Python (ruff + ty). Non-Python repos skip silently. Future work can extend to eslint/tsc for Node repos.
- The gate runs against the entire repo (not just changed files) to catch transitive type errors. This is slower but more correct — a new import can break a downstream file.

## References

- https://docs.astral.sh/ruff/
- https://docs.astral.sh/ty/
