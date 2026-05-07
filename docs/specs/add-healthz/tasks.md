# Tasks — Add /healthz liveness endpoint to the dashboard

> **Spec slug:** `add-healthz`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Add GET /healthz route and test
  - **Implements:** R-001
  - **Touches:** `services/dashboard/src/dashboard/app.py`, `services/dashboard/tests/test_healthz.py`
  - **Done when:** GET /healthz returns HTTP 200 with JSON body {"status": "ok"} and content-type application/json; test in services/dashboard/tests/test_healthz.py passes via `uv run pytest services/dashboard/tests/test_healthz.py`

- [ ] **T-002** — Upgrade ECS container health check to HTTP probe and install curl
  - **Implements:** R-002
  - **Touches:** `terraform/modules/dashboard/ecs.tf`, `services/dashboard/Dockerfile`
  - **Depends on:** T-001
  - **Done when:** The ECS task definition container healthCheck command is ["CMD-SHELL", "curl -sf http://127.0.0.1:8080/healthz"] and the Dockerfile runtime stage installs curl; `docker build` succeeds for services/dashboard/Dockerfile
