# Architect grounding + spec PR iteration

**Status:** Plan / not started.
**Origin:** Investigation triggered by run `019e0393-8807-75f2-98a7-f72a2c086fc1` on issue [smoketurner/ai-dlc#33](https://github.com/smoketurner/ai-dlc/issues/33). The architect produced a Next.js spec for a project that uses FastAPI + Jinja2 + Alpine.js (per `CLAUDE.md`). Spec PR: [smoketurner/ai-dlc#37](https://github.com/smoketurner/ai-dlc/pull/37).

This doc covers two related work streams:

1. **Part A** — fix the architect's grounding so it stops inventing technology choices when the canonical project context is unavailable.
2. **Part B** — extend the state machine so a human can comment on a spec PR with feedback and the architect regenerates the spec into the same PR (mirroring how task PRs already iterate).

Both came out of the same incident; doing A first prevents B from amplifying the problem (each iteration regenerating another wrong spec).

## Diagnostic context

Two grounding sources should have prevented the Next.js choice. Both were empty.

**Source 1 — per-project MEMORY.md S3 snapshot.** `s3 ls s3://ai-dlc-dev-memory-md-022671037892-us-east-1/projects/smoketurner-ai-dlc/` returned nothing. The architect's `read_memory_md` tool fetches `projects/{project_slug}/MEMORY.md` and falls back to an empty string. There is no automatic sync from `docs/MEMORY.md` in the cloned repo to the S3 bucket — the bucket only gets populated by the Proposer agent, which never runs for fresh projects.

**Source 2 — `list_repo_paths` over the cloned repo.** The architect's own `design.md` flagged this in `open_questions`:

> "list_repo_paths was not available in this run — the design assumes a Next.js App Router dashboard based on the project name 'ai-dlc' and typical conventions."

The architect followed the prompt correctly (the prompt says "if `list_repo_paths` returns an empty list, say so in `open_questions` rather than guessing"). The clone *should* have populated `/workspace/repo` before the agent ran (`agents/architect/src/architect/app.py` calls `clone_target_repo` immediately, before `build_agent`). Why the tool returned empty is the open diagnostic question.

The system prompt is correct — it explicitly warns against assuming Next.js for FastAPI projects. The failure mode is empty inputs, not bad reasoning.

---

## Part A — architect grounding (~4-6 hours)

### A1. Diagnose `list_repo_paths` empty return — DONE

**Status:** diagnosed 2026-05-07. Root cause is not what the original hypothesis list assumed.

**Evidence from CloudWatch (`/aws/bedrock-agentcore/runtimes/ai_dlc_dev_architect-5GMyNBF3Oo-DEFAULT`).** Three invocations of run `019e0393-8807-75f2-98a7-f72a2c086fc1`:

| Time (UTC) | Stream | Outcome |
|---|---|---|
| 19:00:18 | `5921d223-…` | `KeyError: AIDLC_GITHUB_APP_SECRET_ARN` — separate, since-fixed deploy bug; container was missing the env var. The clone never ran. |
| 19:01:19 | `91dcbd35-…` | Clone succeeded (`architect cloned target repo path=/workspace/repo target_repo=smoketurner/ai-dlc`). 26.7s later: `Tool #1: SpecBundle` → `spec ready`. **No other tool calls logged.** |
| 19:01:45 | `91dcbd35-…` | Duplicate retry on the same run id — same pattern, also only `Tool #1: SpecBundle`. |

**Root cause.** `agents/architect/src/architect/agent.py:83` calls `agent.structured_output(SpecBundle, user_message)`. Per the Strands SDK source ([`src/strands/agent/agent.py:649`](https://github.com/strands-agents/sdk-python/blob/main/src/strands/agent/agent.py#L649)) this method bypasses the agent loop and the tool registry entirely — it directly invokes `model.structured_output(...)` with the schema as the only available tool. The grounding tools (`read_memory_md_tool`, `list_repo_paths_tool`, `read_repo_file_tool`) and the gating tool (`write_spec_doc_tool`) are never offered to the model on this code path. The `RequirePriorCall` hook in `architect/hooks.py` is also irrelevant because the architect's `write_spec_doc` runs deterministically in `app.py:upload_spec()` (calling the plain Python function, not the Strands tool), and the hook only fires on Strands tool calls.

The architect's `design.md` self-explanation ("list_repo_paths was not available in this run") was the model rationalizing — the tool was never in its toolspec. The clone subprocess succeeded, `/workspace/repo` was populated, and `git ls-files` would have returned non-empty.

**Strands has deprecated this method.** [`Agent.structured_output`](https://strandsagents.com/docs/api/python/strands.agent.agent/index.md#1.10) is marked deprecated; the [recommended pattern](https://strandsagents.com/docs/user-guide/concepts/agents/structured-output/index.md) is `agent(prompt, structured_output_model=SpecBundle)` — that runs the full agent loop with tools, then constrains the final turn to the schema.

**Repo-wide implication.** The same anti-pattern exists in every Strands agent that declares `tools=`:

| Agent | Call site |
|---|---|
| `agents/architect/src/architect/agent.py:83` | `agent.structured_output(SpecBundle, user_message)` |
| `agents/critic/src/critic/agent.py:63` | `agent.structured_output(Critique, user_message)` |
| `agents/reviewer/src/reviewer/agent.py:69` | `agent.structured_output(Review, user_message)` |
| `agents/tester/src/tester/agent.py:70` | `agent.structured_output(Report, user_message)` |
| `agents/proposer/src/proposer/agent.py:100` | `agent.structured_output(Proposal, user_message)` |
| `agents/triage/src/triage/agent.py:52` | `agent.structured_output(TriageDecision, user_message)` (no tools — unaffected) |

**Original hypothesis list — for the record:**

- ✅ `clone_target_repo` ran without error: confirmed by `architect cloned target repo` log line.
- ✅ Clone subprocess exited 0 and `/workspace/repo` was populated: implied by clone success log + lack of subsequent error.
- ✅ `git ls-files` would have returned non-empty: standard shallow clone of an active repo.
- ❌ Agent invoked `list_repo_paths`: **never invoked** — the tool was not in the toolspec.
- ❌ AgentCore microVM workspace recycle: irrelevant.
- ❌ Stale workspace from prior crash: irrelevant (the 19:00:18 failure happened before clone, so workspace was clean).
- ❌ `MAX_LIST_ENTRIES = 200` truncation: irrelevant.
- ❌ Bad `prefix=` filter: irrelevant.

**Required scope change to A2/A3/A4.** The original plan assumed grounding tools are reachable but returning empty. They are unreachable. The fix order shifts:

1. **A1.5 (new, blocker for everything else).** Switch each tool-using Strands agent from the deprecated `agent.structured_output(Model, prompt)` to `agent(prompt, structured_output_model=Model)` and read the result via `result.structured_output`. Touches 5 agents (architect, critic, reviewer, tester, proposer); update tests that mock `agent.structured_output` (`agents/*/tests/test_agent.py`); verify usage-metric extraction in `common.runtime.usage_from_strands` still works against an `AgentResult` rather than a Pydantic instance. Effort: 4-6 hours. **Discuss before implementing** — multi-file, architectural — per the project memory rule.
2. A2 (seed MEMORY.md from clone) becomes load-bearing: only after A1.5 will `read_memory_md` actually be called on each architect run.
3. A3 (fail-closed `RequirePriorCall` content guard) stays — but only once A1.5 lets the hook fire at all.
4. A4 (`read_claude_md` tool) stays optional, post-A2.

**Forks introduced by A1.5:**

- Switch all 5 agents in one PR vs. one PR per agent. Recommend one PR — the change is mechanical and behaviorally identical, splitting just adds review overhead.
- Re-run a regression suite vs. push and watch dev. Recommend regression suite — the agent loop now runs more model turns, which has cost + latency implications.
- Should we keep `RequirePriorCall` in `hooks.py` or replace it with a `BeforeInvocationEvent` content guard? Recommend keep for now — once tool calls actually fire the existing hook is sufficient; the content guard is A3.

### A2. Seed per-project MEMORY.md from the cloned repo

**Effort:** 2-3 hours.

Add a step to `agents/architect/src/architect/app.py` that runs after `clone_target_repo`:

1. Read `docs/MEMORY.md` and `CLAUDE.md` from the cloned repo (if present).
2. Upload to `s3://{memory_md_bucket}/projects/{project_slug}/MEMORY.md` (idempotent — if the S3 object exists and matches, no-op).
3. Subsequent calls to `read_memory_md` then return the synced content.

This is a hot-path side-channel — every architect invocation does the sync. Cheap (one `head_object` + maybe one `put_object`), eliminates cold-start gaps for new projects.

**Fork:** alternative is a separate event-driven sync (GitHub `push` webhook on the target repo's main branch → Lambda → S3). More architecturally clean (sync is independent of architect runs), more infrastructure. Recommendation: A2 inline first; promote to webhook later only if multiple agents need the sync.

### A3. Fail closed when grounding is empty

**Effort:** 1-2 hours.

Tighten the existing `RequirePriorCall(target="write_spec_doc", prerequisite="read_memory_md")` hook in `agents/architect/src/architect/hooks.py`. Today it only checks the tool was *called* — not that it returned content. Add a content guard:

- If `read_memory_md` returned empty (no MEMORY.md found anywhere) AND `list_repo_paths` returned empty (or wasn't called), refuse `write_spec_doc` and return a structured "I cannot proceed without grounding" error.
- The architect's app.py catches this and emits a different event — either `ISSUE.TRIAGED` with `action=ask` (if the run is issue-driven) or fails the run with a clear message.

This is the safety net for cases where A1/A2 don't cover (e.g., target repo has no MEMORY.md AND no recognizable framework files).

**Fork:** strictness — refuse-and-ask, or proceed-but-warn? Refuse is safer (no more wrong specs); warn keeps momentum but puts more burden on the human reviewer. Recommendation: refuse-and-ask, treat the architect as a Triage-class agent in this edge case.

### A4. Add `read_claude_md` tool (optional)

**Effort:** 1 hour.

Many ai-dlc-style projects have a `CLAUDE.md` at the repo root (it's the canonical Claude Code project manifest). It often carries the same stack-preference content as `MEMORY.md` but with broader project context. Add a Strands tool that reads `CLAUDE.md` from the cloned repo, mention it in the system prompt's "Ground in the repo" section.

Lower priority than A1-A3 — `list_repo_paths` + `read_repo_file` already let the architect read CLAUDE.md if it knows to look. A first-class tool just makes the pattern explicit.

---

## Part B — spec PR iteration (~8-12 hours)

### State machine additions

New `RunState.spec_iterating` between `spec_pr_open` and `spec_drafted`. Transition table additions:

| Event | From | To |
|---|---|---|
| `SPEC.ITERATION_REQUESTED` | `spec_pr_open` | `spec_iterating` |
| `SPEC.READY` | `spec_iterating` | `spec_drafted` |

Mid-iteration accumulator: `SPEC.ITERATION_REQUESTED` arriving in `spec_iterating` / `architect_running` / `critic_running` does **not** advance state — it appends to `pending_feedback` and `delivery_ids` on the STATE row, same pattern as the task-iteration accumulator we built in `Pre-deploy hardening / Blocker 2`.

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

1. ~~**A1** — diagnose `list_repo_paths` empty.~~ Done. See updated section above.
2. ~~**A1.5** — migrate all 6 Strands agents off deprecated `agent.structured_output()`.~~ Done. Helper `run_for_structured_output` lives in `packages/common/src/common/runtime.py`; architect / critic / reviewer / tester / proposer / triage now call it. Triage was included even though it has no tools — same deprecation, same fix. All workspace tests + lint + types green.
3. ~~**A2** — seed MEMORY.md from clone.~~ Done. `agents/architect/src/architect/repo_grounding.py:sync_memory_md_from_clone` runs after `clone_target_repo`, reads `docs/MEMORY.md` + `CLAUDE.md`, and uploads a single combined object to `s3://{memory_md_bucket}/projects/{slug}/MEMORY.md`. Idempotent via ETag/MD5 head-object compare; the body intentionally omits a timestamp so identical source content produces a byte-identical body. No-op when neither file exists (preserves the empty-grounding signal A3 will guard on).
4. **A3** — fail-closed grounding hook (1-2h).
5. **A4** — `read_claude_md` tool (1h, optional).
6. Then **Part B** as a single feature batch.

Doing A first prevents Part B from compounding the grounding problem (every spec iteration would still produce a wrong spec until grounding works).

## Open questions

- ~~Why exactly did `list_repo_paths` return empty on the live run?~~ Answered: it was never offered to the model — `agent.structured_output()` bypasses the agent's tool registry.
- A1.5 scope: one bundled PR for all 5 agents vs. per-agent PRs. (Recommend bundled.)
- Does `common.runtime.usage_from_strands` need adjustment when reading metrics off an `AgentResult` rather than a returned Pydantic instance? (Verify in A1.5.)
- Should A2's MEMORY.md sync run inline in the architect or as an event-driven side-channel? (Inline first, promote later if needed.)
- Should A3 refuse to draft entirely when grounding is empty, or proceed-with-warning? (Recommend refuse.)
- Spec iteration: force-push vs new-commit; bot announcement comment yes/no; re-run critic yes/no. (Recommend force-push, post comment, re-run critic.)

## What does NOT change

- Task PR iteration flow (already shipped — this work mirrors it for spec PRs).
- The seven-agent topology, dashboard, and EventBridge bus.
- The architect's system prompt's high-level operating principles (it's correct; the inputs were the problem).
