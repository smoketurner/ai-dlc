# Design — Deterministic lint/typecheck gates between agent steps

> **Spec slug:** `lint-typecheck-gates`

## Approach

Add a quality gate module (`agents/implementer/src/implementer/quality_gate.py`) that runs deterministic static-analysis commands against the working tree after the agent's Claude session completes but before `finalize_task_branch` commits and pushes. The gate reads commands from the target repo's `StackProfile` (already computed by `packages/common/src/common/stack_discovery.py`). On failure, the implementer gets one automatic retry turn (a constrained Claude sub-session with the failure output as context, similar to the existing conflict-resolver pattern). If the retry also fails the gate, the task is blocked.

The gate runs inside the existing implementer container — no new Lambda, no new state machine state, no new EventBridge event. It's a pure in-process check between the agent's edit session and the commit+push step. This keeps the change minimal and avoids adding orchestration complexity.

The gate commands default to the ai-dlc project's own Makefile targets (`uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`) when the target repo is ai-dlc itself, and fall back to `StackProfile.lint_command` / `StackProfile.format_command` for external repos. A missing profile or missing commands means the gate is skipped.

## Components

- **quality_gate** (`agents/implementer/src/implementer/quality_gate.py`) — Runs lint/format/typecheck commands against the working tree and returns structured pass/fail results. Provides the retry prompt composition and the blocked-reason formatter.
- **gate_commands** (`agents/implementer/src/implementer/gate_commands.py`) — Resolves which commands to run for a given target repo by reading the StackProfile from S3 (already fetched during stack discovery) or falling back to well-known defaults for the ai-dlc repo itself.
- **client.py (modified)** (`agents/implementer/src/implementer/client.py`) — Integrates the quality gate into execute_initial and execute_iteration flows — calls the gate after drive_agent returns, handles retry, and threads gate failure into blocked_reason.

## Data model

```text
```
@dataclass(frozen=True)
class GateCommand:
    """One lint/typecheck command to run."""
    name: str          # e.g. "ruff-check", "ruff-format", "ty-check"
    command: str       # e.g. "uv run ruff check ."
    category: str      # "lint" | "format" | "typecheck"

@dataclass(frozen=True)
class GateResult:
    """Result of running one gate command."""
    command: GateCommand
    exit_code: int
    output: str        # combined stdout+stderr, truncated to 4096 chars
    passed: bool

@dataclass(frozen=True)
class GateOutcome:
    """Aggregate result of all gate commands."""
    results: tuple[GateResult, ...]
    all_passed: bool
    retry_prompt: str | None   # composed only when all_passed is False
    blocked_reason: str | None # composed only on second failure
```

No database changes. No new DDB attributes. No new EventBridge events.
```

## Sequence

```text
1. `execute_initial` / `execute_iteration` calls `drive_agent(...)` → agent edits code, calls `finish(status='done')`.
2. If `report.status == 'done'`, call `resolve_gate_commands(target_repo, spec_s3_prefix)` to get the list of `GateCommand`s.
3. If no commands resolved, skip to step 7.
4. Call `run_gate(commands, cwd=repo_path())` → returns `GateOutcome`.
5. If `outcome.all_passed`, skip to step 7.
6. If first attempt: compose a retry prompt from `outcome.retry_prompt`, run a constrained Claude sub-session (max 8 turns, $1 budget, same tools minus finish) that fixes the violations, then re-run the gate. If second gate run passes, proceed to step 7. If it fails, set `blocked_reason = outcome.blocked_reason`.
7. Call `finalize_task_branch(...)` (existing flow — commit + push).
```

## Testing strategy

Unit tests in `agents/implementer/tests/test_quality_gate.py`:
- `test_run_gate_all_pass`: mock subprocess, verify `GateOutcome.all_passed=True`.
- `test_run_gate_lint_fails`: mock subprocess with non-zero exit, verify `GateOutcome.all_passed=False` and `retry_prompt` contains the command + output.
- `test_run_gate_truncates_output`: verify output > 4096 chars is truncated.
- `test_resolve_gate_commands_from_profile`: supply a `StackProfile` with lint/format commands, verify correct `GateCommand` list.
- `test_resolve_gate_commands_no_profile`: verify empty list returned when no profile exists.
- `test_gate_skipped_when_no_commands`: integration-level test that `execute_initial` proceeds without gate when commands list is empty.

Unit tests in `agents/implementer/tests/test_gate_commands.py`:
- `test_aidlc_self_commands`: verify the hardcoded ai-dlc commands are returned when `project_slug == 'ai-dlc'`.
- `test_external_repo_commands`: verify StackProfile-derived commands.

All tests use `subprocess` mocking (no real ruff/ty invocations). Tests live alongside existing implementer tests. Mocks: `subprocess.run` for gate execution, `s3_client().get_object` for profile fetch.

## Failure modes & mitigations

- Gate command hangs (e.g., ty enters an infinite loop on pathological code): mitigated by subprocess timeout (60s per command). On timeout, treat as failure and compose retry prompt.
- Target repo has no recognized stack profile: gate is skipped entirely (AC-007), no regression.
- Retry sub-session introduces new lint violations while fixing old ones: the second gate run catches them and blocks. The human sees both the original and new violations in blocked_reason.

## Trade-offs

- Running the gate inside the implementer container means the container must have the target repo's toolchain installed — for ai-dlc this is already true (uv/ruff/ty are in the base image). For external repos, the gate relies on whatever `mise install` or the sandbox bootstrap set up. If the toolchain isn't available, the gate command fails and the task blocks — acceptable because the same failure would happen in CI.
- One retry turn adds ~$1 of model cost per gate failure. This is much cheaper than a full iteration cycle ($5+ per dispatch) and avoids the 2-5 minute state-machine round-trip.
- The gate runs synchronously in the implementer's thread, adding 5-15 seconds to the task completion time. Acceptable given the implementer session is already 2-10 minutes.

## References

- agents/implementer/src/implementer/client.py — existing execute_initial/execute_iteration flow
- packages/common/src/common/stack_discovery.py — StackProfile model and discovery logic
- agents/implementer/src/implementer/prompts.py — RESOLVER_SYSTEM_PROMPT pattern for constrained sub-sessions
