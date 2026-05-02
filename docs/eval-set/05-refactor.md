# 05 — Refactor

> **Slug:** `refactor`  ·  **Category:** quality

## Intent

> The `echo` service has accumulated direct `os.environ[...]` reads in five places. Refactor to a single `Settings` Pydantic model loaded once at startup. Don't change behaviour. Don't add new env vars. Existing tests must keep passing.

## Setup

`echo` repo with at least 5 routes/modules, each reading env vars directly via `os.environ`.

## Expected behaviour

- Architect writes a 3-5 task refactor spec `centralise-settings`.
- Each task migrates a small set of `os.environ` reads to use the `Settings` instance, with a passing test confirming behaviour is unchanged.
- No task changes external behaviour or the env-var contract.
- Tasks are reviewable in isolation — each PR is a small, focused diff.

## Pass criteria

- 3 ≤ task_count ≤ 6.
- After all PRs merge, `grep -r "os.environ\[" src/` returns zero hits.
- Existing tests keep passing through every PR (the implementer doesn't break the build mid-refactor).
- No new functional acceptance criteria added.
- Total run cost < $5.
