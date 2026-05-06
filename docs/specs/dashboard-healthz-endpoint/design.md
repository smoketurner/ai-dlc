# Design — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `dashboard-healthz-endpoint`

## Approach

Add a new route handler at /healthz in the Next.js App Router (app/healthz/route.ts). The handler responds to GET and HEAD with a static JSON payload and 200 status. No external dependencies are called — this is a pure liveness check. A simple module-level flag (isShuttingDown) is toggled on SIGTERM to flip the response to 503 during graceful shutdown. The endpoint is excluded from any authentication middleware via the existing matcher config.

## Components

- **HealthzRouteHandler** (`dashboard/app/healthz/route.ts`) — Handles GET and HEAD requests to /healthz, returning 200 {"status":"ok"} or 503 {"status":"unavailable"} based on process health
- **ShutdownSignalModule** (`dashboard/lib/shutdown-signal.ts`) — Exports an isShuttingDown flag set to true on SIGTERM; imported by the healthz handler to decide response code
- **HealthzIntegrationTest** (`dashboard/tests/healthz.test.ts`) — Tests the /healthz endpoint returns correct status codes and bodies for healthy and shutting-down states

## Data model

```text
No persistent data. Response schema:

```
interface HealthResponse {
  status: "ok" | "unavailable";
}
```

GET /healthz → 200 { status: "ok" } | 503 { status: "unavailable" }
HEAD /healthz → 200 (empty body) | 503 (empty body)
```

## Sequence

```text
1. Client (ALB / ECS agent) sends GET /healthz
2. Next.js router matches app/healthz/route.ts
3. GET handler imports isShuttingDown from lib/shutdown-signal.ts
4. If isShuttingDown is false → respond 200 {"status":"ok"}
5. If isShuttingDown is true → respond 503 {"status":"unavailable"}
6. No middleware auth check (path excluded in middleware matcher)
```

## Failure modes & mitigations

- If SIGTERM is not propagated to the Node.js process (e.g., PID 1 issue in container), the 503 transition will not fire. Mitigation: ensure the Dockerfile uses exec form or tini as init.
- If middleware matcher is misconfigured, /healthz could require auth and ALB checks would fail. Mitigation: integration test asserts no auth required.

## Trade-offs

- Simplicity over depth: the endpoint only checks process liveness, not downstream connectivity. A separate /readyz endpoint can be added later for readiness.
- Module-level flag means the signal only works within the same process; if Next.js spawns workers, each worker registers its own SIGTERM handler. This is acceptable for ECS single-task deployments.

## References

- https://nextjs.org/docs/app/building-your-application/routing/route-handlers
- https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/
