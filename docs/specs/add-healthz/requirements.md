# Requirements — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `add-healthz`

## Summary

Add a lightweight /healthz HTTP endpoint to the FastAPI dashboard service that ALB target-group health checks (and operators) can hit to confirm the process is alive and serving requests. The endpoint must not require authentication and must return a fixed JSON body with HTTP 200.

## User stories

- **R-001** — As a platform operator, I want hit GET /healthz on the dashboard and receive an HTTP 200 with a JSON body indicating the service is alive so that ALB target-group health checks pass and ECS tasks are kept in service.

## Acceptance criteria

- **AC-001** (R-001) — Given the dashboard process is running, when a GET request is sent to /healthz, then the response status is 200 and the JSON body is {"status": "ok"}.
- **AC-002** (R-001) — Given the dashboard process is running and AIDLC_AUTH is not disabled, when a GET request is sent to /healthz without any authentication headers or cookies, then the response status is still 200 (endpoint is exempt from auth).

## Out of scope

- Deep dependency checks (DB connectivity, S3 reachability) — this is a liveness probe, not a readiness probe
- Changing ALB target-group health-check path in Terraform (can be done in a follow-up)
