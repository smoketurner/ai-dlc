# Requirements — /healthz liveness endpoint for the dashboard

> **Spec slug:** `add-healthz`

## Summary

Add a dedicated, tested /healthz liveness endpoint to the dashboard service. The ALB target group already health-checks this path (terraform/modules/dashboard/alb.tf), and a minimal implementation exists in pages.py, but it lacks test coverage and is co-located with authenticated page routes. This spec extracts it into its own route module with a proper text/plain response and unit test.

## User stories

- **R-001** — As a platform operator, I want hit GET /healthz on the dashboard and receive a 200 text/plain response without authentication so that I can confirm the service is alive via ALB health checks and manual probes.
- **R-002** — As a developer, I want have a unit test covering the /healthz endpoint so that regressions are caught before deploy.

## Acceptance criteria

- **AC-001** (R-001) — Given the dashboard is running, when a GET request is sent to /healthz with no auth headers, then the response status is 200 and Content-Type is text/plain; charset=utf-8 and body is ok.
- **AC-002** (R-001) — Given the dashboard is running behind the ALB with Cognito auth enabled, when the ALB sends a health-check probe to /healthz, then the probe succeeds (200) without triggering the Cognito auth flow.
- **AC-003** (R-002) — Given the test suite is executed, when pytest runs the healthz test module, then the test passes and asserts status 200, content-type, and body.

## Out of scope

- Readiness checks (e.g., verifying DynamoDB connectivity)
- Structured JSON health response with version/commit metadata
- Changes to the ALB target group Terraform (already configured for /healthz)
