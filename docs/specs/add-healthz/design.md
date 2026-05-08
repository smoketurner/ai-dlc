# Design — /healthz liveness endpoint for the dashboard

> **Spec slug:** `add-healthz`

## Approach

Extract the existing /healthz handler from services/dashboard/src/dashboard/routes/pages.py into a new dedicated route module services/dashboard/src/dashboard/routes/healthz.py. The new module defines a single GET /healthz route that returns a PlainTextResponse("ok") with no authentication dependency. Register the new router in services/dashboard/src/dashboard/app.py and remove the old handler from pages.py. Add a unit test in services/dashboard/tests/test_healthz.py.

## Components

- **healthz router** (`services/dashboard/src/dashboard/routes/healthz.py`) — Serves GET /healthz returning 200 text/plain ok with no auth dependency
- **app registration** (`services/dashboard/src/dashboard/app.py`) — Includes the healthz router in the FastAPI app before other routers so it is matched first
- **healthz test** (`services/dashboard/tests/test_healthz.py`) — Verifies status code 200, Content-Type text/plain, and body ok via FastAPI TestClient

## Data model

```text
No data model changes. The endpoint is stateless — it accepts no input and returns a fixed plain-text body.
```

## Sequence

```text
ALB health-check probe → GET /healthz (no auth headers) → FastAPI routes to healthz.router → handler returns PlainTextResponse("ok", status_code=200) → ALB marks target healthy
```

## Failure modes & mitigations

- If the healthz route is accidentally removed or gated behind auth, the ALB will mark all targets unhealthy and the service will become unreachable. The unit test guards against this regression.

## Trade-offs

- Dedicated module vs inline in app.py: A separate module keeps the pattern consistent with other route files (pages.py, runs.py, etc.) and makes the endpoint discoverable.
- PlainTextResponse vs JSONResponse: Liveness probes only need a status code; plain text is simpler and matches the ALB matcher (200). JSON would add unnecessary overhead.

## References

- terraform/modules/dashboard/alb.tf — ALB target group health check configuration (path /healthz, matcher 200)
- services/dashboard/src/dashboard/routes/pages.py — current location of the existing /healthz handler to be removed
