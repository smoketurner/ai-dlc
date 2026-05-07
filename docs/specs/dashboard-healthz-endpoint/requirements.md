# Requirements — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `dashboard-healthz-endpoint`

## Summary

Add a lightweight /healthz HTTP GET endpoint to the dashboard application that returns a 200 OK response with a JSON body indicating the service is alive. This endpoint is intended for use by container orchestrators (ECS, ALB target-group health checks) to determine liveness without authentication.

## User stories

- **R-001** — As a platform operator, I want hit GET /healthz on the dashboard and receive a 200 response when the process is alive so that container orchestrators and load balancers can determine liveness and route traffic correctly.

## Acceptance criteria

- **AC-001** (R-001) — Given the dashboard application is running, when a GET request is made to /healthz, then the response status is 200 and the JSON body is {"status": "ok"}.
- **AC-002** (R-001) — Given the dashboard application is running, when a non-GET request (e.g., POST) is made to /healthz, then the response status is 405 Method Not Allowed.
- **AC-003** (R-001) — Given the dashboard application is running, when a GET request is made to /healthz, then no authentication or authorization is required to receive the 200 response.

## Out of scope

- Deep health checks (database connectivity, downstream service reachability)
- Readiness probes (separate /readyz endpoint)
- Metrics or tracing on the health endpoint

## Open questions

- list_repo_paths was not available in this run — the design assumes a Next.js App Router dashboard based on the project name 'ai-dlc' and typical conventions. If the dashboard uses a different framework (e.g., FastAPI), the implementation path will differ. Please confirm the dashboard framework.
