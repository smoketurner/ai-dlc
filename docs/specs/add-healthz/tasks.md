# Tasks — /healthz liveness endpoint for the dashboard

> **Spec slug:** `add-healthz`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Extract /healthz into dedicated route module with test
  - **Implements:** AC-001, AC-002, AC-003
  - **Touches:** `services/dashboard/src/dashboard/routes/healthz.py`, `services/dashboard/src/dashboard/routes/pages.py`, `services/dashboard/src/dashboard/app.py`, `services/dashboard/tests/test_healthz.py`
  - **Done when:** GET /healthz returns 200 with Content-Type text/plain; charset=utf-8 and body ok; the endpoint requires no authentication (no CurrentUser dependency); the old healthz handler is removed from pages.py; services/dashboard/tests/test_healthz.py passes asserting status code, content-type header, and response body; ruff check and ty pass with no new warnings
