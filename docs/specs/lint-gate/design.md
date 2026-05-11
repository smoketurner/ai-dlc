# Design — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-gate`

## Approach

Insert a deterministic lint/format/type/test gate into the Implementer's execute_task flow — after the Claude Agent SDK session completes (agent called finish with status='done' and made real changes) but before commit_changes / push_branch. The gate runs four Makefile targets sequentially: `make lint`, `make format`, `make type`, `make test`. `make format` auto-applies formatting fixes (ruff format writes changes); the remaining three are pure checks. On any non-zero exit, the combined error output is fed back to the agent as a follow-up message in the same SDK session (the session is kept open until the gate passes or the one-retry budget is exhausted). This avoids the 3–5 minute CI→webhook→event→dispatch round-trip for trivially fixable errors. No new state-machine states, events, or DDB schema changes are needed — the gate is entirely internal to the Implementer container.

## Components

- **LintGate** (`agents/implementer/src/implementer/lint_gate.py`) — Runs the four make targets (lint, format, type, test) via subprocess, collects per-command results, decides pass/fail
- **LintGateResult** (`agents/implementer/src/implementer/lint_gate.py`) — Pydantic model carrying pass/fail, per-command exit codes, retry count, and truncated output for observability
- **execute_initial (modified)** (`agents/implementer/src/implementer/client.py`) — Calls run_lint_gate after the agent loop, retries once on failure before committing
- **execute_iteration (modified)** (`agents/implementer/src/implementer/client.py`) — Same lint-gate integration for iteration runs
- **ImplementerResult (extended)** (`packages/common/src/common/runtime.py`) — Adds optional lint_gate field for downstream observability (dashboard, telemetry)

## Data model

```text
```python
# agents/implementer/src/implementer/lint_gate.py

class CommandResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    command: str                          # e.g. "make lint"
    exit_code: int
    output: Annotated[str, Field(max_length=4096)]  # combined stdout+stderr, truncated

class LintGateResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    passed: bool
    commands: list[CommandResult]         # always 4 entries (lint, format, type, test)
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
  │     ├─ run_lint_gate(repo_root)  →  gate_result
  │     │     runs: make lint, make format, make type, make test
  │     │     (make format auto-fixes; lint/type/test are checks)
  │     │
  │     │  if gate FAILED and retry_budget > 0:
  │     │     ├─ compose_lint_feedback(gate_result) → feedback_prompt
  │     │     ├─ drive_agent(feedback_prompt)  →  (report2, usage2)
  │     │     ├─ merge usage2 into cumulative totals
  │     │     ├─ run_lint_gate(repo_root)  →  gate_result_final
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

All acceptance criteria are verified by unit tests in `agents/implementer/tests/test_lint_gate.py` and extended integration in `agents/implementer/tests/test_client.py`.

- AC-001, AC-004: Mock `subprocess.run`, verify all four commands (`make lint`, `make format`, `make type`, `make test`) are invoked with `cwd=repo_path()` and no shell=True. Assert the commands use `make` targets, not direct `uv run` invocations.
- AC-002: Simulate gate failure (non-zero exit from `make lint`), verify `compose_lint_feedback` produces the expected prompt containing the error output, verify `drive_agent` is called a second time with that prompt.
- AC-003: Simulate double failure, verify `commit_changes` is still called and `LintGateResult.passed=False` is set on the result.
- AC-005: Verify `ImplementerResult.lint_gate` is populated after both pass and fail scenarios with correct `retry_count` and per-command output.
- AC-006: Verify the iteration flow (`execute_iteration`) calls `run_lint_gate` with the same logic.

Mocks: `subprocess.run` is patched to simulate make pass/fail. The Claude Agent SDK's `ClaudeSDKClient` is mocked via the existing test fixture pattern. No live AWS calls needed. Tests live alongside the module under `agents/implementer/tests/`.

## Failure modes & mitigations

- `make` not installed in the Implementer container → gate raises FileNotFoundError; caught by the outer try/except in run_implementer, emits RUN.FAILED. Mitigated: the Dockerfile already has make installed as a build dep.
- `make test` hangs on a large test suite → subprocess.run timeout (60s per command). The gate treats timeout as a failure and proceeds to commit.
- Agent produces more lint errors on retry → gate fails, commit proceeds with passed=False. CI catches the residual.
- `make format` writes changes that conflict with the agent's intent → unlikely since format is deterministic; if it happens, the subsequent `make lint` / `make type` will catch regressions.

## Trade-offs

- One retry only: More retries would increase session cost and risk infinite loops on genuinely unfixable type errors. One retry catches 90%+ of trivial issues while bounding cost.
- Gate runs all four commands even if one fails early: Running all four gives the agent complete feedback in one shot rather than requiring serial fix→retry cycles per tool.
- `make format` auto-applies changes: Unlike the original design which avoided auto-fix, the Makefile convention uses `make format` to write changes. This is acceptable because the agent still must fix lint/type/test errors itself — only formatting is auto-applied.
- Truncated output at 4 KiB per command: Prevents pathological cases from blowing up the context window.
- `make test` included in the gate: Adds ~10-30s but catches import errors and basic regressions before the PR is pushed, reducing CI iteration cycles.

## References

- https://docs.astral.sh/ruff/
- https://docs.astral.sh/ty/
