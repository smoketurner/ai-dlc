# 04 — Bug fix

> **Slug:** `bug-fix`  ·  **Category:** maintenance

## Intent

> The `/healthz` route is returning 500 when the build SHA env var is missing. It should return 200 with `build_sha: null`. Add a regression test that uses a clean env without `BUILD_SHA` set.

## Setup

`echo` repo with the `/healthz` route from case 01. There's an open issue (or the user has reproduced the bug locally) showing the 500.

## Expected behaviour

- Architect writes a 1-2 task spec `fix-healthz-missing-build-sha`. No ADR — this is local code, not a cross-cutting decision.
- Implementer's PR fixes the route + adds the regression test.
- The PR description references the failing acceptance criterion in `requirements.md`.

## Pass criteria

- 1 ≤ task_count ≤ 2.
- The regression test fails on the pre-fix code (the implementer can demonstrate this by checking `git log` or running the test against the old code).
- Total run cost < $1.50.
- Total wall-clock duration < 20 minutes.
- No new ADR.
