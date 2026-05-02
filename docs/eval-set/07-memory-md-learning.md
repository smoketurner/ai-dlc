# 07 — MEMORY.md learning

> **Slug:** `memory-md-learning`  ·  **Category:** memory

## Intent

> Add a `POST /uppercase` endpoint to the `echo` service that takes `{"text": "..."}` and returns `{"result": "..."}` uppercased.

## Setup

`echo` repo. `docs/MEMORY.md` has a Conventions bullet that says **"All non-trivial routes must include a contract test using the platform's `pytest-fastapi-fixtures`."** No existing reference to that fixture.

## Expected behaviour

- The Architect reads `MEMORY.md` and notices the contract-test convention.
- The spec includes a task to write a contract test using `pytest-fastapi-fixtures`, even though the user's intent didn't mention it.
- If `pytest-fastapi-fixtures` isn't an existing dep, the spec includes a task to add it and updates the MEMORY.md Notes.

## Pass criteria

- A test using `pytest-fastapi-fixtures` exists for the new route.
- `MEMORY.md` is read in the architect's session log (the agent's tool calls show the read).
- No prompt-engineering required — the architect inferred the convention from MEMORY.md alone.
- Total run cost < $3.
