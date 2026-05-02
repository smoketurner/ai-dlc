# 01 — Empty-repo bootstrap

> **Slug:** `empty-repo-bootstrap`  ·  **Category:** baseline

## Intent

> Bootstrap a Python web service called "echo" with a single `GET /healthz` endpoint that returns `{"ok": true, "build_sha": "<sha>"}`. Use FastAPI, uv, ruff, ty, pytest. Pin every dependency to an exact version. Add a multi-stage `Dockerfile` for `linux/arm64`. Add a `docs/MEMORY.md` with the conventions section pre-filled.

## Setup

The project repo `echo` exists on GitHub as an empty (or near-empty) repository. No `pyproject.toml`, no `src/`, no Dockerfile. The dashboard sees a project_slug `echo` with no prior runs.

## Expected behaviour

- The Architect produces a single spec `bootstrap-echo` whose tasks list covers, in order: scaffold `pyproject.toml` and `uv.lock`, add the FastAPI app + `GET /healthz`, add a unit test, add the Dockerfile, seed `docs/MEMORY.md`, wire CI lint+type+test.
- The Implementer opens **one PR per task**. Tasks chain in order. No task touches files outside its scope.
- All deps pinned to exact versions; ruff + ty + pytest configured strict.

## Pass criteria

- 5 ≤ task_count ≤ 8.
- Every PR title is `T-NNN: …` and merges cleanly into `main`.
- Final repo: `uv sync && uv run ruff check . && uv run ty check && uv run pytest -q` all pass with zero warnings.
- `docker build --platform linux/arm64 -t echo .` succeeds.
- Total run cost < $5.
- No PR touches files outside its task's `Touches` list.
