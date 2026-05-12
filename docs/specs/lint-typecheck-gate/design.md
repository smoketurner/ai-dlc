# Design — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-typecheck-gate`

## Approach

The lint/typecheck gate runs inside the Implementer container, after the Claude Agent SDK session ends with status='done' but before the TASK.READY event is emitted. This keeps the gate within the existing `implementer_running` task state — no new state machine states needed.

The flow becomes:
1. Claude Agent SDK session completes with `finish(status='done')`
2. `execute_task` calls a new `run_lint_gate(...)` function
3. `run_lint_gate` resolves commands from the stack profile (cached in the spec S3 prefix or discovered from the working tree)
4. Each command runs via `subprocess.run` in the checked-out repo
5. On failure: feed output back to a constrained Claude sub-session (like the conflict resolver) that only has Edit + Bash tools, capped at MAX_GATE_RETRIES
6. On success (or skip): return to the normal flow which emits TASK.READY
7. On exhausted retries: return a blocked result so the app emits TASK.BLOCKED

The gate commands are resolved from a new `LintGateConfig` that reads the target repo's stack profile (already discovered by the Architect and stored alongside the spec). For ai-dlc itself, this resolves to `uv run ruff check .` and `uv run ty check`.

## Components

- **LintGateConfig** (`packages/common/src/common/lint_gate.py`) — Resolve which lint/typecheck commands to run for a given repo, based on the stack profile or MEMORY.md conventions. Returns an ordered list of shell commands.
- **run_lint_gate** (`agents/implementer/src/implementer/lint_gate.py`) — Execute the lint/typecheck gate commands in the Implementer's working tree. Returns pass/fail with captured output. Orchestrates retries by invoking a constrained Claude sub-session on failure.
- **GATE_FIXER_SYSTEM_PROMPT** (`agents/implementer/src/implementer/prompts.py`) — System prompt for the constrained sub-session that fixes lint/typecheck violations. Scoped to Edit + Bash only, no new files, no test runs.
- **build_gate_fixer_options** (`agents/implementer/src/implementer/options.py`) — Build ClaudeSDKClient options for the gate-fixer sub-session with restricted tool access (Edit, Bash, Read only).

## Data model

```text
No new DynamoDB tables or attributes. The gate runs entirely within the Implementer container's lifecycle.

```python
@dataclass(frozen=True)
class LintGateResult:
    passed: bool
    commands_run: list[CommandResult]
    retries_used: int

@dataclass(frozen=True)
class CommandResult:
    command: str
    exit_code: int
    output: str  # last 4096 bytes of stdout+stderr combined
```

The `LintGateConfig` resolves commands from the stack profile:
```python
@dataclass(frozen=True)
class LintGateConfig:
    lint_commands: tuple[str, ...]  # e.g. ('uv run ruff check .',)
    typecheck_commands: tuple[str, ...]  # e.g. ('uv run ty check',)
    max_retries: int  # default 3, from AIDLC_GATE_MAX_RETRIES env
```
```

## Sequence

```text
1. `execute_task` / `execute_iteration` calls `drive_agent(...)` → agent finishes with `status='done'`
2. `compute_blocked_reason(...)` returns None (agent not blocked)
3. NEW: `run_lint_gate(repo_path, config)` is called
   a. Resolves `LintGateConfig` from stack profile in `/workspace/spec/` or working tree
   b. Runs each command via `subprocess.run(shell=True, cwd=repo_path, capture_output=True)`
   c. If all pass → returns `LintGateResult(passed=True, ...)`
   d. If any fail → composes a retry prompt with the failure output
   e. Invokes a constrained Claude sub-session (gate fixer) with Edit+Bash
   f. After fixer completes, re-runs all gate commands
   g. Repeats up to `max_retries` times
   h. On final failure → returns `LintGateResult(passed=False, ...)`
4. If gate passed: proceed to `finalize_task_branch` + merge (existing flow)
5. If gate failed: set `blocked_reason` from gate output, proceed to blocked path
```

## Testing strategy

Unit tests in `agents/implementer/tests/test_lint_gate.py`:
- AC-001/AC-003: Mock `subprocess.run` to return exit 0 for all commands; assert `run_lint_gate` returns `passed=True` and the caller proceeds to emit TASK.READY.
- AC-002/AC-007: Mock `subprocess.run` to return exit 1 with stderr output; assert the gate composes a retry prompt containing the command name, exit code, and output tail; mock the fixer sub-session to simulate a fix; assert re-run passes.
- AC-004: Mock `subprocess.run` to always fail; assert that after `max_retries` the result is `passed=False` and `blocked_reason` is set.
- AC-005: Unit tests for `LintGateConfig.from_stack_profile(...)` with various stack profiles (Python/uv → ruff+ty, Node → eslint, no lint → empty).
- AC-006: Assert that when `LintGateConfig` resolves to zero commands, `run_lint_gate` returns `passed=True` immediately.

Unit tests in `packages/common/tests/test_lint_gate.py`:
- Test `LintGateConfig` resolution from `StackProfile` objects with different language/toolchain combinations.

All tests are pure unit tests using `unittest.mock.patch` for subprocess calls and Claude SDK invocations. No integration tests needed — the gate is deterministic shell commands.

## Failure modes & mitigations

- Gate command hangs (subprocess timeout): each command gets a 120s timeout; on TimeoutExpired the gate treats it as a failure and feeds the timeout to the fixer.
- Fixer sub-session introduces new lint violations while fixing old ones: the full gate re-runs after each fixer pass, catching regressions.
- Stack profile missing from spec S3 prefix: fall back to discovering from the working tree via common.stack_discovery.discover_stack.
- Gate passes but subsequent merge conflicts undo the fix: the existing merge-conflict resolver handles this; the gate only guarantees the task branch is clean at commit time.

## Trade-offs

- Running the gate inside the Implementer container (not as a separate Lambda) means gate failures don't produce observable events — but this avoids new state machine complexity and keeps the feedback loop sub-second.
- Using a Claude sub-session for fixes costs tokens but is more reliable than regex-based auto-fix for type errors that require code changes.
- The gate runs only lint+typecheck, not the full test suite — tests remain in CI. This keeps the gate fast (<30s) at the cost of not catching test failures pre-PR.

## References

- https://docs.astral.sh/ruff/
- https://docs.astral.sh/ty/
