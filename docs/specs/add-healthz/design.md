# Design — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `add-healthz`

## Approach

Add a minimal FastAPI route at GET /healthz that returns {"status": "ok"} with a 200 status code. The route is registered directly on the app (not behind any auth middleware). Because the dashboard's auth is handled at the ALB/Cognito level (not in FastAPI middleware), no special auth-bypass logic is needed — the route simply needs to exist and respond. A dedicated route module keeps it isolated and testable.

## Components

- **healthz router** (`services/dashboard/src/dashboard/routes/healthz.py`) — Defines GET /healthz returning a fixed JSON response; registered on the FastAPI app before any auth-dependent middleware.
- **app registration** (`services/dashboard/src/dashboard/app.py`) — Includes the healthz router in the FastAPI application.
- **healthz tests** (`services/dashboard/tests/test_healthz.py`) — Verifies the endpoint returns 200 with the expected JSON body using the FastAPI TestClient.

## Data model

```text
Response schema (not persisted):

```json
{"status": "ok"}
```

No database or state changes.
```

## Sequence

```text
1. ALB (or operator) sends `GET /healthz` to the dashboard container on port 8080.
2. FastAPI routes the request to `healthz.router`.
3. The handler returns `JSONResponse({"status": "ok"}, status_code=200)` immediately — no I/O, no auth check.
4. ALB marks the target healthy.
```

## Failure modes & mitigations

- If the uvicorn process is dead or OOM-killed, the TCP connection will fail and ALB will drain the target — this is the desired behaviour.
- If the route is accidentally removed or the import fails, the app will still start (other routes work) but ALB health checks will 404 and drain the target. CI tests guard against this.

## Trade-offs

- Using a dedicated router module (vs. inlining in app.py) adds one file but keeps the pattern consistent with all other routes in the project.
- Returning a static dict means the probe cannot detect downstream failures — acceptable for a liveness check; a readiness probe can be added later if needed.

## References

- services/dashboard/src/dashboard/app.py
- services/dashboard/src/dashboard/routes/pages.py
- services/dashboard/Dockerfile
