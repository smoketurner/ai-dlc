# Implementation blocked: T-002

> **spec_slug:** `add-healthz` · **task:** `T-002`

## Blocker

agent produced no diff

## How to advance

- **Continue**: comment on this PR with `@aidlc-bot <guidance>` to retry the implementation with that guidance as feedback.
- **Abort this task**: close this PR. Other tasks in the run (if any) keep running.

## Agent summary

Replace the TCP socket health check in the ECS task definition with `curl -sf http://127.0.0.1:8080/healthz` so container liveness tracks application-level availability rather than port reachability. Add `curl` to the runtime stage of the dashboard Dockerfile so the CMD-SHELL command is available in the container image.

## Risks the agent flagged

- curl adds ~3 MB to the runtime image; acceptable per design trade-off documented in design.md
- docker build not run in CI here — build correctness depends on the Dockerfile syntax being valid and the apt package name being correct (curl is standard in debian:trixie-slim)
