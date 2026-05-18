# Agent Catalog

Eight agents drive the ai-dlc pipeline. All ship as `linux/arm64` container images on Bedrock AgentCore Runtime.

## Triage

- **Model**: Claude Haiku 4.5
- **Framework**: Strands Agents
- **Role**: Classifies issue-driven runs. Only runs for runs triggered by GitHub issues (programmatic runs skip straight to Architect).
- **Verdicts**: `proceed`, `ask`, `defer`, `decline`, `research`
- **Input**: Issue URL, number, title, body, labels, triggering comment (if any)
- **Output**: `ISSUE.TRIAGED` event with action, rationale, confidence, and a decision S3 key

`proceed` advances to Architect. `research` branches to Proposer. `ask`/`defer`/`decline` terminate the run with a comment on the issue.

## Architect

- **Model**: Claude Opus 4.6
- **Framework**: Strands Agents
- **Role**: Writes a structured `plan.md` to S3. The plan is an internal artifact -- not committed to git.
- **Plan sections**: Context, Assumptions, Approach, Files, Reuse, Implementation steps, Verification, Out of scope
- **Input**: The request intent, target repo context, `MEMORY.md`, skills preamble, stack profile
- **Output**: `DESIGN.READY` event with `plan_s3_key` and summary
- **Tools**: `artifact_tool` (S3 read/write), `repo_helper` (file tree, file read)

If a plan surfaces an architectural choice with multi-run reach, the Implementer commits a new ADR under `docs/ADRs/` as part of the impl PR.

## Implementer

- **Model**: Claude Sonnet 4.6
- **Framework**: Claude Agent SDK
- **Role**: Opens the single impl PR for the run. Runs in two modes:
  - `mode=implementation` -- initial pass: clone repo, read plan from S3, implement, open PR on branch `aidlc/impl/{run_id}`
  - `mode=revision` -- revision pass: clone repo, check out impl branch, read validator artifacts from S3, apply fixes, push
- **Tools**: Bash, WebFetch, WebSearch (outbound network access), `artifact_tool`, `repo_helper`
- **Output**: `IMPL_PR.OPENED` (initial) or `REVISION.READY` (revision)
- **Guardrails**: Deny-list enforced by hooks (no `rm -rf /`, no force-push to main, no `terraform apply` against prod, no secrets in code, etc.)

Container credentials are scoped to Bedrock + project S3 + GitHub App for the target repo. The only path code reaches the repo is a human-reviewed PR.

## Reviewer

- **Model**: Claude Sonnet 4.6
- **Framework**: Strands Agents
- **Role**: Code-reviews the impl PR. Its verdict gates the run.
- **Verdicts**: `approve`, `request_changes`, `comment`
- **Input**: PR diff, plan, issue context (title + body for assumption checks)
- **Output**: `REVIEW.READY` event with verdict, severity counts, summary
- **Special behavior**: Performs per-assumption checks -- verifies each architect assumption against the source issue text. Posts review comments on the PR via `repo_helper.comment_pr`.

`approve`/`comment` check CI state and advance toward merge. `request_changes` triggers a revision (under the cap).

## Tester

- **Model**: Claude Haiku 4.5
- **Framework**: Strands Agents
- **Role**: Flags test gaps in the impl PR. Advisory only -- does not gate the run.
- **Input**: PR diff, plan
- **Output**: `TEST_REPORT.READY` event with gap count, suggested test count, summary
- **Special behavior**: Enumerates existing tests before listing gaps. Posts findings on the PR.

## Code-Critic

- **Model**: Claude Opus 4.6
- **Framework**: Strands Agents
- **Role**: Adversarial review of the impl PR against the **original GitHub issue**. Advisory only.
- **Input**: PR diff, plan, issue context (title + body)
- **Output**: `CODE_CRITIQUE.READY` event with issue count, severity counts, summary, critique S3 key
- **Finding lenses**: `issue->diff` (does the diff satisfy the issue?), `user-problem` (does it solve the user's actual problem?), `plan-drift` (did the impl drift from the plan?), `edge-case` (missed edge cases)
- **Severity tags**: Each finding is severity-tagged (high/medium/low)

## Proposer

- **Model**: Claude Opus 4.6
- **Framework**: Strands Agents
- **Role**: Handles research-path runs (when triage classifies as `research`). Reads external docs and opens PRs proposing prompt or `MEMORY.md` edits.
- **Input**: Issue context, current MEMORY.md, prompts
- **Output**: `RUN.COMPLETED` event (after opening proposal PRs)

## Retrospector

- **Model**: Claude Haiku 4.5
- **Framework**: Strands Agents
- **Role**: Fires on every terminal event (capture mode) and weekly per destination (consolidate mode).
- **Two modes**:
  - **Capture** -- fires on PR-signal events (`IMPL_PR.OPENED`, `REVIEW.READY`, `CHECKS.PASSED`, `CHECKS.FAILED`, `IMPL.ITERATION_REQUESTED`, plus terminal events). Emits zero or more scored bullets to AgentCore Memory under a stable session (`pending_lessons:{destination}[:{slug}]`). No PR opened.
  - **Consolidate** -- fires weekly per destination (via `consolidate_schedule` EventBridge rule). Reads all pending events, ranks them, opens up to two PRs (one for `MEMORY.md` additions, one for new skills), lists `shipped_event_ids` + `discarded_event_ids` for cleanup.
- **Destinations**: `target_repo` (repo-specific lessons) or `platform` (agent-friction signals, validator false-positives, missing-tool symptoms)

## Gateway Tools

Two Lambdas serve as AgentCore Gateway targets, providing tools to all agents:

### `artifact_tool`

S3 + `MEMORY.md` operations:
- Read/write plan artifacts
- Read/write validator outputs
- Read `MEMORY.md` files (root + nested)
- Read stack profiles

### `repo_helper`

Git/GitHub operations:
- Clone repos, read file trees, read files
- Open PRs, comment on PRs, post review comments
- `get_check_state(pr_url)` -- aggregates GitHub Check state for a PR to determine if checks passed/failed/pending

## Adding a New Agent

Seven-step process:

1. `cp -r agents/architect agents/<name>` and rename module + Dockerfile entrypoint.
2. Add the package as a workspace member (no action needed if under `agents/*`).
3. Implement the agent in `src/<name>/agent.py` and the AgentCore Runtime shell in `src/<name>/app.py`.
4. Register the agent in `terraform/modules/agents/variables.tf` (`var.agents`); apply (creates IAM, gateway, workload identity -- no ECR repo, no runtime yet).
5. Push the image via the `images-build` workflow. The ECR repo `${project}/<name>` is auto-created on first push by `aws_ecr_repository_creation_template.agents`.
6. Add `<name> = "latest"` to `agent_image_tags` in `terraform/envs/<env>/main.tf` and apply again to create the AgentCore Runtime.
7. Add the corresponding state(s) to `packages/common/src/common/state.py`, transitions to `state_transitions.py`, and a dispatch handler in `lambdas/state_router/src/state_router/dispatch_run.py` -- only if the agent participates in the run state machine (out-of-band agents like the Retrospector skip this step).

## Runtime Details

- All agents use `container_configuration` (not `code_configuration`) because Python 3.14 is outside AgentCore's supported range (3.10-3.13).
- Images are built as `linux/arm64` and pushed to ECR with immutable tags (except `latest`).
- Each agent has a `prompts.py` with `SYSTEM_PROMPT`. A/B testing is supported via `prompts_b.py` alongside `prompts.py` -- the `routing.py` module deterministically picks variant per `(run_id, agent_name)`.
- The `app.py` module in each agent is the AgentCore Runtime entrypoint (listens on `:8080/invocations`).
