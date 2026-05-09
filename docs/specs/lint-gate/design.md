# Design — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-gate`

## Approach

Insert a deterministic lint/type-check gate into the Implementer's execute_task flow — after the Claude Agent SDK session completes (agent called finish) but before commit_changes / push_branch. The gate runs three commands sequentially (uv run ruff check ., uv run ruff format --check ., uv run ty check) from the repo root. On failure, the combined error output is fed back to the agent as a follow-up message in the same SDK session (the session is kept open until the gate passes or the retry budget is exhausted). This avoids the 3–5 minute CI→webhook→event→dispatch round-trip for trivially fixable errors. No new state-machine states, events, or DDB schema changes are needed — the gate is entirely internal to the Implementer container.

## Components

- **LintGate** (`agents/implementer/src/implementer/lint_gate.py`) — Runs the three lint/type commands via subprocess, collects per-command results, decides pass/fail
- **LintGateResult** (`agents/implementer/src/implementer/lint_gate.py`) — Pydantic model carrying pass/fail, per-command exit codes, and truncated output for observability
- **execute_initial (modified)** (`agents/implementer/src/implementer/client.py`) — Calls run_lint_gate after the agent loop, retries once on failure before committing
- **execute_iteration (modified)** (`agents/implementer/src/implementer/client.py`) — Same lint-gate integration for iteration runs
- **ImplementerResult (extended)** (`packages/common/src/common/runtime.py`) — Adds optional lint_gate field for downstream observability (dashboard, telemetry)

## Data model

```text
```python
# agents/implementer/src/implementer/lint_gate.py

class CommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    command: str                          # e.g. "uv run ruff check ."
    exit_code: int
    output: Annotated[str, Field(max_length=4096)]  # combined stdout+stderr, truncated

class LintGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    passed: bool
    commands: list[CommandResult]         # always 3 entries
    retry_count: Annotated[int, Field(ge=0, le=1)]  # 0 = first pass, 1 = after retry

# packages/common/src/common/runtime.py — ImplementerResult extension
class ImplementerResult(_UsageMixin):
    ...
    lint_gate: LintGateResult | None = None  # None when agent reported blocked
```
```

## Sequence

```text
```
execute_initial / execute_iteration
  │
  ├─ drive_agent(user_prompt)  →  (report, usage)
  │
  ├─ [if report.status == 'done' and agent_made_real_changes]:
  │     ├─ run_lint_gate(repo_path())  →  gate_result
  │     │
  │     │  if gate FAILED and retry_budget > 0:
  │     │     ├─ compose_lint_feedback(gate_result) → feedback_prompt
  │     │     ├─ drive_agent(feedback_prompt)  →  (report2, usage2)
  │     │     ├─ merge usage2 into cumulative totals
  │     │     ├─ run_lint_gate(repo_path())  →  gate_result_final
  │     │     └─ update report from report2 if available
  │     │
  │     └─ attach gate_result to ImplementerResult.lint_gate
  │
  ├─ commit_changes(...)
  ├─ push_branch(...)
  └─ open_pr(...)  →  pr_url
```
```

## Testing strategy

All acceptance criteria are verified by unit tests in `agents/implementer/tests/test_lint_gate.py` and extended tests in `agents/implementer/tests/test_client.py`.

- AC-001, AC-004: Mock `subprocess.run`, verify all three commands are invoked with `uv run` prefix and `cwd=repo_path()`.
- AC-002: Simulate gate failure (non-zero exit from ruff), verify `compose_lint_feedback` produces the expected prompt containing the error output, verify `drive_agent` is called a second time with that prompt.
- AC-003: Simulate double failure, verify `commit_changes` is still called and `LintGateResult.passed=False` is set on the result.
- AC-005: Verify `ImplementerResult.lint_gate` is populated after both pass and fail scenarios.
- AC-006: Verify the iteration flow (`execute_iteration`) calls `run_lint_gate` with the same logic.

Mocks: `subprocess.run` is patched to simulate lint pass/fail. The Claude Agent SDK's `ClaudeSDKClient` is mocked via the existing test fixture pattern. No live AWS calls needed.

## Failure modes & mitigations

- uv not installed in the Implementer container → gate raises FileNotFoundError; caught by the outer try/except in run_implementer, emits RUN.FAILED. Mitigated: the Dockerfile already installs uv.
- ty check hangs on a large repo → subprocess.run timeout (30s per command). The gate treats timeout as a failure and proceeds to commit.
- Agent produces more lint errors on retry → gate fails, commit proceeds with passed=False. CI catches the residual.

## Trade-offs

- One retry only: More retries would increase session cost and risk infinite loops on genuinely unfixable type errors. One retry catches 90%+ of trivial lint issues while bounding cost.
- Gate runs all three commands even if one fails early: Running all three gives the agent complete feedback in one shot rather than requiring serial fix→retry cycles per tool.
- No --fix auto-apply: We could run ruff check --fix automatically, but this masks the training signal — the agent should learn to produce clean code.
- Truncated output at 4 KiB per command: Prevents pathological cases from blowing up the context window.

## References

- https://docs.astral.sh/ruff/
- https://docs.astral.sh/ty/
