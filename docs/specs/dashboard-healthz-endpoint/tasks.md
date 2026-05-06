# Tasks — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `dashboard-healthz-endpoint`

Ordered, atomic units. Each task is one PR.

- [x] **T-001** — Add shutdown signal module
  - **Implements:** AC-003
  - **Touches:** `dashboard/lib/shutdown-signal.ts`
  - **Done when:** dashboard/lib/shutdown-signal.ts exports a boolean `isShuttingDown` that flips to true on SIGTERM; unit test confirms the flag toggles when SIGTERM is emitted.

- [ ] **T-002** — Implement /healthz route handler
  - **Implements:** AC-001, AC-002, AC-003, AC-004
  - **Touches:** `dashboard/app/healthz/route.ts`
  - **Depends on:** T-001
  - **Done when:** dashboard/app/healthz/route.ts exports GET and HEAD handlers; GET returns 200 {"status":"ok"} when healthy and 503 {"status":"unavailable"} when shutting down; HEAD returns matching status with empty body; no downstream calls are made.

- [ ] **T-003** — Exclude /healthz from auth middleware
  - **Implements:** AC-001, AC-004
  - **Touches:** `dashboard/middleware.ts`
  - **Depends on:** T-002
  - **Done when:** The middleware matcher in dashboard/middleware.ts (or equivalent config) excludes /healthz so unauthenticated requests are served; existing auth behavior for other routes is unchanged.

- [ ] **T-004** — Add integration tests for /healthz
  - **Implements:** AC-001, AC-002, AC-003, AC-004
  - **Touches:** `dashboard/tests/healthz.test.ts`
  - **Depends on:** T-003
  - **Done when:** dashboard/tests/healthz.test.ts contains tests verifying: GET returns 200 with correct JSON; HEAD returns 200 with empty body; response when shutdown flag is true returns 503; response latency is under 10 ms; no auth header required. All tests pass in CI.
