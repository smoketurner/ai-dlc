# Requirements — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `dashboard-healthz-endpoint`

## Summary

Expose a lightweight /healthz HTTP GET endpoint on the dashboard service that returns a 200 OK response with a minimal JSON body, enabling container orchestrators and load balancers to verify the process is alive.

## User stories

- **R-001** — As a platform operator, I want send an HTTP GET to /healthz on the dashboard and receive a 200 response when the process is healthy so that container orchestrators (ECS, ALB target-group health checks) can confirm the dashboard is alive and route traffic to it.
- **R-002** — As a platform operator, I want receive a non-200 response from /healthz when the dashboard process cannot serve requests so that unhealthy containers are drained and replaced automatically.

## Acceptance criteria

- **AC-001** (R-001) — Given the dashboard service is running and able to serve requests, when an HTTP GET request is sent to /healthz, then the response status is 200, Content-Type is application/json, and the body is {"status":"ok"}.
- **AC-002** (R-001) — Given the dashboard service is running, when an HTTP HEAD request is sent to /healthz, then the response status is 200 with no body.
- **AC-003** (R-002) — Given the dashboard service is in a degraded state (e.g., event loop blocked or shutting down), when an HTTP GET request is sent to /healthz, then the response status is 503 with body {"status":"unavailable"}.
- **AC-004** (R-001) — Given the dashboard service is running, when an HTTP GET request is sent to /healthz, then the response latency is under 10 ms and no downstream dependencies are called.

## Out of scope

- Deep health checks that verify database or cache connectivity (readiness probe)
- Authentication or authorization on the /healthz endpoint
- Metrics emission from the health endpoint itself

## Open questions

- Is the dashboard built with Next.js API routes, FastAPI, or another framework? The spec assumes Next.js App Router based on typical ai-dlc patterns; if different, the file paths will need adjustment.
- Should the ALB target-group health check path be updated in Terraform as part of this spec, or is that managed separately?
