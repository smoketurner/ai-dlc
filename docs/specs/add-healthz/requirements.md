# Requirements — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `add-healthz`

## Summary

Add a GET /healthz liveness endpoint to the dashboard FastAPI app that returns HTTP 200 with no authentication required. The ALB target group already health-checks this path (terraform/modules/dashboard/alb.tf), but the route does not exist in the application. This spec closes that gap and upgrades the ECS container health check from a raw TCP socket probe to an HTTP GET against /healthz for better readiness signal.

## User stories

- **R-001** — As a platform operator, I want the dashboard to expose a dedicated /healthz endpoint so that the ALB target group can reliably determine container health.
- **R-002** — As a platform operator, I want the ECS container health check to use an HTTP probe against /healthz so that it detects application-level failures (not just port availability).

## Acceptance criteria

- **AC-01** (R-001) — Given the dashboard app is running, when a GET request is made to /healthz without any auth headers, then the response is HTTP 200 with JSON body {"status": "ok"}.
- **AC-02** (R-001) — Given the dashboard app is running, when a GET request is made to /healthz, then the response includes content-type: application/json.
- **AC-03** (R-002) — Given the ECS task definition is applied, when the container health check executes, then it performs an HTTP GET to http://127.0.0.1:8080/healthz instead of a TCP socket connect.

## Out of scope

- Deep health checks (database connectivity, downstream service reachability)
- Readiness probes (separate /readyz endpoint)
- Startup probes or graceful-shutdown signalling
