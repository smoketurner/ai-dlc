# Roadmap

Live tracker for the AI-DLC build.

The platform's eight agents (Architect, Implementer, Reviewer, Tester, Code-Critic, Triage, Proposer, Retrospector), the FastAPI dashboard, and the SQS-beacon + DDB-state orchestration are all in place. Step Functions and the four legacy orchestration Lambdas (`hitl_handler`, `runtime_invoker`, `iteration_reactor`, `triage_dispatcher`) have been removed in the SQS cutover. The eval pipeline (state machine + drift detector + GitHub Actions workflow) was also removed. The plan-stage Critic + the spec PR + per-task PRs have all been removed in favor of the single-impl-PR-per-issue model.

**Current focus:** failure-context plumbing for revisions. Workflow Run logs flow into the validator dispatch payload as `ci_failure_excerpt` so the reviewer grounds CI-failure claims in real output; the same excerpt + the triggering `@aidlc-bot` comment land in S3 as `r{N}-checks.md` and `r{N}-mention.md` so the implementer's revision pass has the full context. Previous focus — validator grounding (Reviewer assumption checks, Tester enumerate-before-gaps, auto-dispatch validators on `IMPL_PR.OPENED`) — shipped.

Legend: ✅ done · 🟡 in progress · ⬜ todo

---

## Pipeline shape

```
REQUEST.RECEIVED
  → ISSUE.TRIAGED        (Triage classifies issue-driven runs — Haiku 4.5)
  → DESIGN.READY         (Architect writes plan.md to S3 — Opus 4.6)
  → IMPL_PR.OPENED       (Implementer pushes one branch, opens one PR — Sonnet 4.6)
  → REVIEW.READY         (Reviewer — Sonnet 4.6, gates the run)
  → TEST_REPORT.READY    (Tester — Haiku 4.5, advisory)
  → CODE_CRITIQUE.READY  (Code-Critic — Opus 4.6, advisory; PR vs. original issue)
  → CHECKS.PASSED        (all required GitHub Checks green)
                           ↓
                       awaiting_human_merge
                           ↓
  → RUN.COMPLETED        (human merges the impl PR)
```

Revisions branch off via `CHECKS.FAILED`, `IMPL.ITERATION_REQUESTED` (human `@aidlc-bot` mention), or a reviewer `request_changes` verdict — the implementer runs in `mode=revision` on the same branch, emits `REVISION.READY`, and validators re-run.

The state cursor is no longer a separate enum — `decide()` in `state_router/decide.py` is a pure function over the event log. The `state_router` Lambda reads the run's events on each SQS-beacon delivery and dispatches the next side-effect (agent invoke, `repo_helper` call, event emit). The `event_projector` writes the events to DDB; no router or projector advances state on its own initiative.

---

## In flight 🟡

- ⬜ **CI logs in validator dispatch payload.** New `repo_helper.get_workflow_run_logs(repo, pr_number)` op fetches the failing job's log tail (head + tail, capped ~8 KB). `lambdas/state_router/src/state_router/payload.py` calls it when emitting `VALIDATORS.DISPATCHED` if checks are red, and threads `ci_failure_excerpt` + `ci_red: bool` into `ReviewerInput` / `TesterInput` / `CodeCriticInput`.
- ⬜ **Write `r{N}-mention.md` and `r{N}-checks.md` to S3.** The webhook handler at `services/dashboard/src/dashboard/routes/webhooks.py` writes the comment body when emitting `IMPL.ITERATION_REQUESTED` and the log excerpt when emitting `CHECKS.FAILED`. Closes the gap where the implementer's `fetch_revision_inputs` reads these keys but nobody writes them.

---

## Deferred (no concrete trigger yet)

- **Plan-stage critic revisited.** Removed for now — the Reviewer's per-assumption check covers the same failure mode post-implementation. Revisit if we observe ≥2 runs where the Reviewer rebuts a load-bearing assumption that the Implementer already wasted a pass on.
- **Implementer judgement — partial matches read as "done".** Observed example: an existing `/healthz` handler in `pages.py` returning HTMLResponse satisfied the implementer that the plan was already implemented, even though the plan asked for a separate `routes/healthz.py` returning `JSONResponse({"status": "ok"})`. Likely a prompt tweak in `agents/implementer/src/implementer/prompts.py` — "a route already existing isn't proof the plan is satisfied; confirm response shape + module path against the design".
- **Stuck-run detector.** Risk: a beacon loss leaves a run stuck silently. CloudWatch alarm on `non_terminal_runs_with_stale_last_event > 0` until a scheduled detector is built.
- Switch AgentCore Runtime to VPC mode.
- Migrate to AgentCore Harness when GA.
- A2A protocol for cross-team or third-agent invocation.
- Slack-based HITL approvals.
- Playwright E2E tests for the dashboard.
- Custom domain for the dashboard.
- Tighten alarm thresholds against real dev traffic.
- Per-run cost cap.
- Auto-merge for impl PRs that pass all validators + all required Checks (currently humans gate every merge).

---

## Decided not to do

Don't re-propose without a trigger that wasn't true at the time of the decision.

- ~~Cedar / Verified Permissions for cross-agent RBAC~~ — per-agent IAM + resource tags are sufficient.
- ~~Langfuse / Datadog OTEL backend~~ — CloudWatch is the trace backend.
- ~~Multi-account AWS Org / Control Tower~~ — single account with env separation is the plan.
- ~~`common/personas/` shared snippet module~~ — used by zero callers; agent prompts have inline equivalents.

---

## How to use this file

1. When you finish a checkbox, mark it `[x]` in the same PR that contains the change.
2. When a section is fully checked, update its header from 🟡 to ✅.
3. Work that comes out of execution but doesn't belong to the current focus: drop it in **Deferred**, ideally with a corresponding GitHub issue.
4. Don't gold-plate. Promote items from Deferred only when there's a concrete trigger.
