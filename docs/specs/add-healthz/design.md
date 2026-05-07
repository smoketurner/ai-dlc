# Design — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `add-healthz`

## Approach

Add a minimal FastAPI route at GET /healthz that returns {"status": "ok"} with no dependency on authentication or any external service. Register it directly on the app object in services/dashboard/src/dashboard/app.py (no separate router needed for a single infrastructure route). Update the ECS container health check command in terraform/modules/dashboard/ecs.tf to use an HTTP GET via curl instead of a raw TCP socket connect. Install curl in the runtime stage of services/dashboard/Dockerfile.

## Components

- **healthz route** (`services/dashboard/src/dashboard/app.py`) — Returns HTTP 200 {"status": "ok"} for ALB and container health checks with no auth required
- **ECS health check update** (`terraform/modules/dashboard/ecs.tf`) — Switches container health check from TCP socket to HTTP GET /healthz for application-level liveness signal
- **Dockerfile curl install** (`services/dashboard/Dockerfile`) — Ensures curl is available in the runtime image for the container health check CMD-SHELL command
- **healthz test** (`services/dashboard/tests/test_healthz.py`) — Verifies the endpoint returns 200 with expected JSON body and content-type, no auth required

## Data model

```text
No new data model. The response is a static JSON object: {"status": "ok"}.
```

## Sequence

```text
ALB health check timer fires (every 15s)
  → GET /healthz (no auth headers — ALB sends health checks directly to target, bypassing listener auth actions)
  → FastAPI app.get("/healthz") handler
  → Return JSONResponse({"status": "ok"}, status_code=200)
  → ALB marks target healthy (matcher=200)

ECS container health check (every 15s)
  → CMD-SHELL: curl -sf http://127.0.0.1:8080/healthz
  → Exit 0 on HTTP 2xx → container marked healthy
```

## Failure modes & mitigations

- If the FastAPI app fails to start (import error, missing env var), /healthz will be unreachable → ALB marks unhealthy after 3 failures (45s) → ECS replaces the task. This is correct behaviour.
- If curl is missing from the image, the container health check fails → ECS marks unhealthy. The Dockerfile change ensures curl is installed.

## Trade-offs

- Static response vs. deep check: A static 200 is appropriate for a liveness probe. Deep checks (DB, EventBridge) belong in a separate /readyz endpoint — mixing them into liveness causes cascading restarts when a downstream is temporarily unavailable.
- curl dependency in runtime image: Adding curl increases image size by ~3 MB. The alternative (a Python one-liner with urllib) avoids the dependency but is slower to execute and harder to read. curl is the standard ECS health check tool.

## References

- terraform/modules/dashboard/alb.tf — ALB target group health_check block already expects GET /healthz → 200
- terraform/modules/dashboard/ecs.tf — ECS task definition container healthCheck block (currently TCP socket)
- services/dashboard/src/dashboard/app.py — FastAPI app entrypoint where the route is registered
- services/dashboard/Dockerfile — runtime stage where curl must be added
