# 02 — Small feature add

> **Slug:** `small-feature-add`  ·  **Category:** day-to-day

## Intent

> In the `echo` service, add a `GET /version` route that returns `{"version": "<pyproject-version>", "commit": "<git-sha>"}`. Update `requirements.md` for it, wire a unit test, and update the README.

## Setup

The `echo` repo is in the state produced by case 01 — bootstrapped, lint-clean, with one `/healthz` route and a CI workflow.

## Expected behaviour

- Architect writes a 2-3 task spec `add-version-route`: implement the route + read pyproject metadata; add a test; update README.
- Tasks reference the existing health-check route and design pattern from `docs/MEMORY.md`.
- Implementer opens 2-3 PRs in sequence.

## Pass criteria

- 2 ≤ task_count ≤ 4.
- Every PR adds tests or updates README; none modifies the `/healthz` route.
- Final repo: `uv run pytest -q` passes; `curl /version` returns the expected JSON shape.
- Total run cost < $2.
- Total wall-clock duration < 30 minutes.
