# Design — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `dashboard-healthz-endpoint`

## Approach

Add a Next.js App Router route handler at app/healthz/route.ts that responds to GET requests with a 200 JSON payload {"status": "ok"}. The handler is a single file with no external dependencies beyond Next.js itself. It explicitly exports only a GET handler so that other HTTP methods receive an automatic 405 from Next.js. No middleware or authentication wraps this route.

## Components

- **healthz route handler** (`dashboard/app/healthz/route.ts`) — Responds to GET /healthz with 200 {"status": "ok"} for liveness probing
- **healthz integration test** (`dashboard/__tests__/healthz.test.ts`) — Verifies the endpoint returns 200 on GET and 405 on POST

## Data model

```text
No persistent data. Response schema: { "status": "ok" } (Content-Type: application/json).
```

## Sequence

```text
1. ALB / ECS agent sends GET /healthz
2. Next.js routes to app/healthz/route.ts GET handler
3. Handler returns NextResponse.json({ status: 'ok' }, { status: 200 })
4. Caller receives 200 with JSON body
```

## Failure modes & mitigations

- If the Next.js process is unresponsive, the health check will time out and the orchestrator will restart the task — this is the desired behavior.
- If the route file is accidentally deleted, the endpoint returns 404 and the orchestrator marks the task unhealthy — caught by the integration test in CI.

## Trade-offs

- Chose a static JSON response over computing real readiness (DB ping) to keep latency < 5 ms and avoid false negatives from transient downstream issues.
- Placed route inside the App Router (app/) rather than pages/api/ to align with Next.js 13+ conventions.

## References

- https://nextjs.org/docs/app/building-your-application/routing/route-handlers
