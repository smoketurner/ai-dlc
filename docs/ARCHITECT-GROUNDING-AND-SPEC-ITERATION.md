# Architect grounding + spec PR iteration

**Status:** Part A partially done (A1 / A1.5 / A2 shipped); A3 / A4 / Part B open.
**Origin:** Investigation triggered by run `019e0393-8807-75f2-98a7-f72a2c086fc1` on issue [smoketurner/ai-dlc#33](https://github.com/smoketurner/ai-dlc/issues/33). The architect produced a Next.js spec for a project that uses FastAPI + Jinja2 + Alpine.js. Spec PR: [smoketurner/ai-dlc#37](https://github.com/smoketurner/ai-dlc/pull/37).

Two related work streams from the same incident:

1. **Part A** — fix the architect's grounding so it stops inventing technology choices when the canonical project context is unavailable.
2. **Part B** — extend the state machine so a human can comment on a spec PR with feedback and the architect regenerates the spec into the same PR (mirroring how task PRs already iterate).

## Already shipped

- **A1 — diagnosed `list_repo_paths` empty.** Root cause: `agent.structured_output(Model, prompt)` bypasses the agent loop and tool registry; grounding tools were never offered to the model on that code path. Strands has deprecated this call.
- **A1.5 — migrated all 6 Strands agents off deprecated `structured_output()`.** Helper `run_for_structured_output` lives in `packages/common/src/common/runtime.py`; architect / critic / reviewer / tester / proposer / triage all call it. Tools now actually fire.
- **A2 — seeded MEMORY.md from clone.** `agents/architect/src/architect/repo_grounding.py:sync_memory_md_from_clone` runs after `clone_target_repo`, reads `docs/MEMORY.md` + `CLAUDE.md` from the clone, uploads to `s3://{memory_md_bucket}/projects/{slug}/MEMORY.md`. ETag/MD5 idempotent. No-op when neither file exists (preserves the empty-grounding signal A3 will guard on).

## Part A — remaining

### A3. Fail closed when grounding is empty

**Effort:** 1-2 hours.

Tighten `RequirePriorCall(target="write_spec_doc", prerequisite="read_memory_md")` in `agents/architect/src/architect/hooks.py`. Today it only checks the tool was *called* — not that it returned content. Add a content guard:

- If `read_memory_md` returned empty (no MEMORY.md found anywhere) AND `list_repo_paths` returned empty (or wasn't called), refuse `write_spec_doc` and return a structured "I cannot proceed without grounding" error.
- The architect's `app.py` catches this and emits a different event — either `ISSUE.TRIAGED` with `action=ask` (if the run is issue-driven) or fails the run with a clear message.

Safety net for cases A1.5/A2 don't cover (e.g., target repo has no `MEMORY.md` AND no recognizable framework files).

**Fork:** strictness — refuse-and-ask, or proceed-but-warn? Refuse is safer; warn keeps momentum but puts more burden on the human reviewer. Recommendation: refuse-and-ask, treat the architect as a Triage-class agent in this edge case.

### A4. Add `read_claude_md` tool (optional)

**Effort:** 1 hour.

Many ai-dlc-style projects have a `CLAUDE.md` at the repo root (canonical Claude Code project manifest). Often carries the same stack-preference content as `MEMORY.md` but with broader project context. Add a Strands tool that reads `CLAUDE.md` from the cloned repo, mention it in the system prompt's "Ground in the repo" section.

Lower priority — `list_repo_paths` + `read_repo_file` already let the architect read CLAUDE.md if it knows to look. A first-class tool just makes the pattern explicit.

---

## Part B — spec PR iteration (~8-12 hours)

### State machine additions

New `RunState.spec_iterating` between `spec_pr_open` and `spec_drafted`. Transition table additions:

| Event | From | To |
|---|---|---|
| `SPEC.ITERATION_REQUESTED` | `spec_pr_open` | `spec_iterating` |
| `SPEC.READY` | `spec_iterating` | `spec_drafted` |

Mid-iteration accumulator: `SPEC.ITERATION_REQUESTED` arriving in `spec_iterating` / `architect_running` / `critic_running` does **not** advance state — it appends to `pending_feedback` and `delivery_ids` on the STATE row, same pattern as the task-iteration accumulator.

### New event type

`SPEC.ITERATION_REQUESTED`. Payload:

| Field | Type |
|---|---|
| `project_slug` | string |
| `spec_slug` | string |
| `spec_s3_prefix` | string |
| `pr_url` | string |
| `delivery_id` | string (X-GitHub-Delivery for idempotency) |
| `feedback` | `FeedbackItem` discriminated union |

Files to touch:

- `packages/common/src/common/events.py` — payload class + literal union.
- `terraform/shared/schemas/SPEC_ITERATION_REQUESTED.json` — JSON schema.
- `terraform/modules/messaging/locals.tf` — registry entry.

### Webhook handlers

`services/dashboard/src/dashboard/routes/webhooks.py` currently ignores spec PR comments / reviews — extend three handlers:

- `handle_pull_request_review` — on `state=changes_requested` against a spec PR (`sk=STATE` row), emit `SPEC.ITERATION_REQUESTED` with `ReviewChangesRequestedFeedback`. Currently returns `"not a task PR"` and exits.
- `classify_pr_comment` — on `@aidlc-bot` mention against a spec PR, emit `SPEC.ITERATION_REQUESTED` with `IssueCommentMentionFeedback`. Currently `is_task` gates this branch.
- `handle_pull_request_review_comment` — on `@aidlc-bot` mention in spec PR review comments, emit `SPEC.ITERATION_REQUESTED` with `ReviewCommentMentionFeedback`. Currently `attr(row, "sk") == "STATE"` exits.

All three should also call `react_to_issue_comment` / `react_to_pr_review_comment` on the comment id.

### Projector additions

`lambdas/event_projector/src/event_projector/handler.py`:

- Move `pending_feedback` + `delivery_ids` accumulator logic from task-row-only to also handle run-level. Need similar `accumulate_iteration_in_place` for the run STATE row.
- Add a `SPEC.READY`-from-`spec_iterating` clear (mirror the existing `TASK.READY`-from-`iterating` clear in `apply_task_ready_clauses`).

### State router dispatch

`lambdas/state_router/src/state_router/dispatch.py`:

- New `handle_spec_iterating(run)` → `invoke_architect(run, advance_from=spec_iterating, advance_to=architect_running)`. The architect payload populates `prior_feedback` from `run.pending_feedback`.
- `handle_spec_critiqued` branches on `run.pr_url`: `None` → first time, call `open_spec_pr`; set → iteration, call new `update_spec_pr`.

### repo_helper new op

`lambdas/repo_helper/src/repo_helper/handler.py`:

- `update_spec_pr` op: reads regenerated docs from S3, force-pushes a new commit to the existing spec branch (PR keeps its number, GitHub auto-updates with the new commits). Optionally posts a comment on the PR ("🔄 Architect regenerated based on this feedback: …") so the timeline reads cleanly.

### Architect (no interface change)

`ArchitectInput.prior_feedback` already exists. Verify the architect actually uses it on regeneration — the prompt has a section for it but we should sanity-check the regenerated spec actually addresses the feedback.

### Tests

- Event payload validation (`packages/common/tests/test_events.py`).
- Webhook handler tests for each of the three new trigger paths.
- Projector tests: state advance + accumulator queue + `SPEC.READY` clear.
- Dispatch test for `handle_spec_iterating`.
- repo_helper test for `update_spec_pr`.

### Forks for B

- Force-push on existing branch (cleanest UX, same PR number) **vs** new commit on top (preserves old commits, uglier diff). Recommend force-push — matches how task iterations behave.
- Bot comment on regenerate (`"🔄 Architect regenerated based on your feedback"`) **vs** silent. Recommend post the comment — makes the PR timeline self-explanatory.
- Re-run critic on iterations **vs** skip. Recommend re-run — the new spec might have new issues; saving one critic invocation isn't worth the risk.

---

## Sequencing

1. **A3** — fail-closed grounding hook (1-2h).
2. **A4** — `read_claude_md` tool (1h, optional).
3. Then **Part B** as a single feature batch.

A3 first prevents Part B from compounding the grounding problem (every spec iteration would still produce a wrong spec until the empty-grounding case fails closed).

## Open questions

- Should A3 refuse to draft entirely when grounding is empty, or proceed-with-warning? (Recommend refuse.)
- Spec iteration: force-push vs new-commit; bot announcement comment yes/no; re-run critic yes/no. (Recommend force-push, post comment, re-run critic.)

## What does NOT change

- Task PR iteration flow (already shipped — Part B mirrors it for spec PRs).
- The seven-agent topology, dashboard, and EventBridge bus.
- The architect's system prompt's high-level operating principles (it's correct; the inputs were the problem).
