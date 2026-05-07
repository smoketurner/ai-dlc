# Tasks — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `dashboard-healthz-endpoint`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Add GET /healthz route handler and integration test
  - **Implements:** AC-001, AC-002, AC-003
  - **Touches:** `dashboard/app/healthz/route.ts`, `dashboard/__tests__/healthz.test.ts`
  - **Done when:** GET /healthz returns 200 {"status":"ok"}, POST /healthz returns 405, and the integration test passes in CI.
