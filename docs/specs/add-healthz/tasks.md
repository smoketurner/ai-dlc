# Tasks — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `add-healthz`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Add GET /healthz route and tests
  - **Implements:** AC-001, AC-002
  - **Touches:** `services/dashboard/src/dashboard/routes/healthz.py`, `services/dashboard/src/dashboard/app.py`, `services/dashboard/tests/test_healthz.py`
  - **Done when:** GET /healthz returns 200 with body {"status": "ok"} and `uv run pytest services/dashboard/tests/test_healthz.py` passes with at least two test cases covering the acceptance criteria.
