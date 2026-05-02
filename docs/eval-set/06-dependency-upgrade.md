# 06 — Dependency upgrade

> **Slug:** `dependency-upgrade`  ·  **Category:** maintenance

## Intent

> Upgrade FastAPI from 0.110 to the latest stable release. Update `pyproject.toml`, regenerate `uv.lock`, fix any breaking changes that surface in tests, and document the upgrade in `MEMORY.md` Notes.

## Setup

`echo` repo with `fastapi==0.110.0` (deliberately old). Tests pass on the current version.

## Expected behaviour

- Architect's spec is small: one task to bump + relock + run tests; one task per breaking change discovered (variable, often zero); one task to update MEMORY.md.
- Architect reads MEMORY.md to learn the project's "exact-pinned deps" convention and conforms to it.

## Pass criteria

- The new FastAPI version is the latest stable as of the run (no `>=` ranges).
- `uv.lock` is regenerated and committed.
- Existing tests pass; if any new tests are added, they explain why in the PR body.
- `MEMORY.md` Notes section gains a one-line upgrade entry.
- Total run cost < $3.
