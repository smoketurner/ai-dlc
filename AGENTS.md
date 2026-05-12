# ai-dlc — Project manifest

An agentic SDLC platform built on AWS Bedrock AgentCore.

## Tech stack

- **Python 3.14** with the Astral toolchain (`uv` workspace, `ruff`, `ty`).
- **Agents**: Strands Agents (Architect, Critic, Code-Critic, Reviewer, Tester, Triage, Proposer, Retrospector) and Claude Agent SDK (Implementer), all shipped as `linux/arm64` containers on Bedrock AgentCore Runtime.
- **Models**: Architect / Critic / Code-Critic / Proposer → Claude Opus 4.6. Implementer / Reviewer → Claude Sonnet 4.6. Tester / Triage / Retrospector / memory consolidation → Claude Haiku 4.5.
- **Orchestration**: SQS-beacon + DynamoDB-state machine driven by a single `state_router` Lambda. The `event_projector` Lambda is the only writer of run/task state.
- **Eventing**: Amazon EventBridge (custom bus + schema registry), DynamoDB streams.
- **Memory**: AgentCore Memory (semantic + summarization strategies) plus per-project `MEMORY.md` files in the AgentCore Runtime persistent filesystem (snapshotted to S3).
- **Dashboard**: FastAPI + Jinja2 + Alpine.js (CDN, no JS build) on API Gateway + Lambda with Cognito OIDC auth.
- **Auth**: Amazon Cognito (single user pool covers ALB + API Gateway).
- **HITL**: GitHub PR reviews/comments → webhook → EventBridge event → `event_projector` advances DDB state → `state_router` dispatches the next side-effect.
- **IaC**: Terraform only (`hashicorp/aws ~> 6`).

## Key directories

| Path | Role |
|------|------|
| `packages/common/` | Shared library. Event envelopes (`events.py`, `event_emit.py`), state machine (`state.py`, `state_transitions.py`), routing rules (`routing.py`), AgentCore wrappers (`agentcore_*.py`), boto3 helpers (`ddb.py`, `s3.py`, `runs.py`), `MEMORY.md` utility (`memory_md.py`), settings (`settings.py`). |
| `agents/architect/` | Strands agent — writes the three-doc spec bundle (requirements + design + tasks). |
| `agents/critic/` | Strands agent — adversarially reviews the spec (advisory). |
| `agents/code_critic/` | Strands agent — adversarially reviews the integrated impl PR (advisory; runs in parallel with reviewer + tester). |
| `agents/implementer/` | Claude Agent SDK agent — opens code PRs; also runs `mode=revision` to apply validator feedback directly onto the impl branch. |
| `agents/reviewer/` | Strands agent — code-reviews the unified impl PR once tasks are complete. Its verdict gates the run. |
| `agents/tester/` | Strands agent — flags test gaps in the unified impl PR (advisory). |
| `agents/triage/` | Strands agent — classifies issue-driven runs (`proceed` / `ask` / `defer` / `decline`). |
| `agents/proposer/` | Strands agent — research-driven (issue → triage classifies as `research`); opens PRs proposing prompt or MEMORY.md edits. |
| `agents/retrospector/` | Strands agent — fires on every terminal event (PR merge, PR close, issue close); appends lessons to `MEMORY.md` via PR. |
| `lambdas/entry_adapter/` | API Gateway → DDB run row + EventBridge `REQUEST.RECEIVED` + SQS beacon. |
| `lambdas/state_router/` | SQS beacon consumer; reads DDB state and dispatches the next side-effect (agent invoke, repo op, event emit). Never writes state. |
| `lambdas/event_projector/` | EventBridge events → DDB state advance (sole writer of `current_state`) + AgentCore Memory `CreateEvent`. |
| `lambdas/artifact_tool/` | AgentCore Gateway target — S3 + `MEMORY.md` ops. |
| `lambdas/repo_helper/` | AgentCore Gateway target — git/GitHub ops. |
| `lambdas/telemetry/` | Categorises `SPEC.REJECTED` / `TASK.REJECTED` events for downstream learning. |
| `lambdas/retrospector_dispatcher/` | EventBridge → AgentCore Runtime invocation for the Retrospector on every terminal event. |
| `services/dashboard/` | FastAPI submission/tracking UI. |
| `terraform/modules/` | Reusable Terraform modules (one per concern). |
| `terraform/envs/dev/` | Environment composition (prod TBD). |
| `terraform/bootstrap/` | One-time S3 + DDB state backend. |
| `docs/ADRs/` | Architectural Decision Records (written by the Architect agent). |
| `MEMORY.md` | Canonical human-reviewed project memory. |

## Memory model

`MEMORY.md` carries repository-scoped context (conventions, ADR bullets, constraints) and is reviewed in PRs. AgentCore Memory carries cross-session facts (user preferences, learned signals) and session events (≤60 days). Sync is one-way: MEMORY.md → AgentCore Memory on every successful session via `CreateEvent`. The reverse only happens through agent-proposed PR edits — humans gate writes to MEMORY.md.

## Request lifecycle

One request → many state transitions, all coordinated through DynamoDB + SQS + EventBridge. The two-Lambda split is load-bearing: `state_router` only reads DDB and triggers side-effects; `event_projector` is the sole writer of `current_state`. This keeps state machine logic in one place and makes every transition observable as an EventBridge event.

1. **Entry**: API Gateway or GitHub webhook → `entry_adapter` writes the run row to DDB, emits `REQUEST.RECEIVED` on EventBridge, sends an SQS beacon.
2. **Dispatch**: `state_router` consumes the beacon, reads `current_state` from DDB, looks up the handler in `dispatch.py` / `dispatch_run.py` / `dispatch_task.py`, and executes the side-effect (invoke AgentCore Runtime, call a repo op, emit an event). Never writes state.
3. **Agent work**: the invoked agent emits one or more domain events (e.g. `SPEC.PROPOSED`, `TASK.IMPLEMENTED`) back to EventBridge.
4. **Projection**: `event_projector` consumes the event, advances `current_state` per `state_transitions.py`, calls AgentCore Memory `CreateEvent`, and enqueues the next SQS beacon if the new state needs dispatch.
5. **HITL**: GitHub PR review/comment → webhook → EventBridge event → same `event_projector` path. Humans gate state advance the same way agents do.
6. **Terminal events** (PR merged/closed, issue closed) fan out via `retrospector_dispatcher` to the Retrospector agent for the lesson-extraction pass.

The run-level state cursor (`RunState` in `packages/common/src/common/state.py`) walks one path of this diagram. Exact event→state transitions are encoded in `RUN_TRANSITIONS` (`state_transitions.py`):

```mermaid
stateDiagram-v2
    [*] --> received: REQUEST.RECEIVED
    received --> triaging
    triaging --> triage_decided: ISSUE.TRIAGED

    triage_decided --> spec_pending: action=proceed
    triage_decided --> proposer_running: action=research
    triage_decided --> done: action=defer / decline

    spec_pending --> architect_running
    architect_running --> spec_drafted: SPEC.READY
    spec_drafted --> critic_running
    critic_running --> spec_critiqued: CRITIQUE.READY
    spec_critiqued --> spec_pr_open
    spec_pr_open --> spec_approved: SPEC.APPROVED
    spec_pr_open --> spec_pending: SPEC.ITERATION_REQUESTED
    spec_pr_open --> failed: SPEC.REJECTED

    spec_approved --> tasks_in_progress
    tasks_in_progress --> tasks_complete
    tasks_complete --> validation_running: dispatch reviewer + tester + code-critic
    validation_running --> validation_complete: REVIEW.READY
    validation_complete --> awaiting_human_merge: verdict=approve / comment
    validation_complete --> revising: verdict=request_changes (under cap)
    validation_complete --> failed: verdict=request_changes (cap hit)
    revising --> validation_running: REVISION.READY
    awaiting_human_merge --> done: RUN.COMPLETED (impl PR merged)

    proposer_running --> done: RUN.COMPLETED
```

`RUN.FAILED` and `RUN.CANCEL_REQUESTED` are wildcard transitions: they advance any non-terminal state to `failed` or `cancelled` respectively. `TaskState` is the per-task cursor (`pending → implementer_running → pr_open → iterating → merged / closed / failed / blocked`) that the run-level `tasks_in_progress` state iterates over. Per-task advisors are gone — reviewer/tester/code-critic now run **once per validation pass** against the integrated impl PR.

### Validation lifecycle

Once every task has reached `pr_open` (its commit is on the impl branch), the run advances to `tasks_complete` and the state-router dispatches three validators in parallel against the unified impl PR:

- **Reviewer** (Sonnet 4.6) — code review with a binary verdict. Drives the next state transition.
- **Tester** (Haiku 4.5) — test-gap analysis. Advisory; informs the reviewer + implementer.
- **Code-Critic** (Opus 4.6) — adversarial review for integration-level gaps, drift from spec intent. Advisory.

All three write Markdown artifacts to `s3://{artifacts_bucket}/runs/{run_id}/validation/{kind}-r{N}.md` where `N` is the revision number (0 for the first pass, 1+ after each implementer revision).

Reviewer's `REVIEW.READY` carries a `verdict`:

- `approve` / `comment` → `awaiting_human_merge`. The human merges PR → `RUN.COMPLETED` → `done`.
- `request_changes` → `revising`. The state-router invokes the implementer in `mode=revision`: clone the repo, check out the impl branch directly (no task branch), read all three validator artifacts from S3, commit fixes onto the impl branch, push. Emits `REVISION.READY` → back to `validation_running`.

The revision loop is capped at `MAX_REVISIONS = 3` (in `dispatch_run.py`); exceeding the cap emits `RUN.FAILED` rather than spending tokens indefinitely. Human intervention (mention `@aidlc-bot` or merge anyway) takes over from there.

## Adding a new agent

1. `cp -r agents/architect agents/<name>` and rename module + Dockerfile entrypoint.
2. Add the package as a workspace member (no action needed if it lives under `agents/*`).
3. Implement the agent in `src/<name>/agent.py` and the AgentCore Runtime shell in `src/<name>/app.py`.
4. Register the agent in `terraform/modules/agents/variables.tf` (`var.agents`); apply (creates IAM, gateway, workload identity — no ECR repo, no runtime yet).
5. Push the image via the `images-build` workflow. The ECR repo `${project}/<name>` is auto-created on first push by `aws_ecr_repository_creation_template.agents` with the standard config (immutable except `latest`, lifecycle policy, AgentCore-pull policy).
6. Add `<name> = "latest"` to `agent_image_tags` in `terraform/envs/<env>/main.tf` and apply again to create the AgentCore Runtime.
7. Add the corresponding state(s) to `packages/common/src/common/state.py`, transitions to `packages/common/src/common/state_transitions.py`, and a dispatch handler in `lambdas/state_router/src/state_router/dispatch.py` — only if the agent participates in the run state machine (out-of-band agents like the retrospector skip this step).

## Target-repo prerequisites

The platform writes branches under `aidlc/spec/*`, `aidlc/impl/*`, and
`aidlc/task/*` and opens PRs from them. Every target repo must be
configured as follows:

- **Settings → General → "Automatically delete head branches"**: enable.
  Spec branches (`aidlc/spec/{slug}`) and impl branches
  (`aidlc/impl/{slug}/{short_run_id}`) are removed on PR merge so old
  runs don't accumulate.
- **Branch protection on `aidlc/*`**: do not configure. The state
  router uses GitHub's server-side merge API (`POST /merges`) to push
  task commits into the run's impl branch; branch protection would
  block it.

Task branches live under `aidlc/task/...` rather than nested inside
`aidlc/impl/...` because git can't have one ref name be a path prefix
of another ref name. Task branches are deleted inline by
`repo_helper.merge_branch` (which passes `delete_head_on_merge=True`)
the moment their merge into the impl branch succeeds. Blocked tasks
keep their branch so reviewers can read `BLOCKED.md`; cancelled-run
branches are rare and operator-cleaned.

## Running tests

```bash
uv run pytest -q                                     # unit tests only
uv run pytest -m integration                          # moto-backed integration
uv run pytest -m live_aws tests/integration/...       # full end-to-end against dev account (gated)
```

## Deploying

Image build and Terraform apply are GitHub Actions workflows. Production applies require manual approval via GitHub Environments.

```bash
gh workflow run images-build.yml --ref main           # all eight agents → ECR
gh workflow run dashboard-build.yml --ref main         # dashboard container → ECR + ECS update-service
gh workflow run terraform-apply.yml --ref main         # apply (dev auto, prod gated)
```

## Local development

Each agent runs unchanged on a laptop — Strands' `Agent` and Claude Agent SDK's `ClaudeSDKClient` produce the same behaviour locally and on AgentCore Runtime.

```bash
cd agents/architect && uv sync && AIDLC_ENV=dev uv run python -m architect.app
# Hit it on :8080/invocations with the Bedrock AgentCore session header.
```

The dashboard runs locally with `uv run uvicorn dashboard.app:app --reload --port 8080` from `services/dashboard/`. Cognito OIDC is bypassed in dev mode (set `AIDLC_AUTH=disabled`).

## Terraform (local)

`terraform.tfvars` and `backend.hcl` are gitignored. On first checkout, copy the `.example` template and supply the partial backend config:

```bash
cd terraform/envs/dev
cp terraform.tfvars.example terraform.tfvars        # then fill in real values
terraform init -reconfigure \
  -backend-config="bucket=<state-bucket-name>" \
  -backend-config="profile=aidlc-admin"
```

`-reconfigure` is required the first time after `backend.tf` was converted to a partial config. The state bucket name and AWS profile can also live in a gitignored `terraform/envs/dev/backend.hcl` invoked via `-backend-config=backend.hcl`. CI supplies `bucket` from the `TF_STATE_BUCKET` repo variable and leaves `profile` empty so the S3 backend falls through to OIDC env-var creds.

## Lint, type, test policy

Inherits the global `~/.claude/CLAUDE.md` standards (≤100 lines/function, complexity ≤8, ≤5 positional params, 100-char lines, absolute imports, Google-style docstrings, zero warnings). Project-specific overrides: none.

## Implementer guardrails (deny-list, enforced by hooks)

- Any `rm -rf /`, `rm -rf $HOME`, `chmod -R 777`, `git push --force-with-lease origin main`.
- Any `aws iam *Delete*`, `terraform apply` against `prod`, `kubectl delete`, `dropdb` / `DROP TABLE`.
- Direct GitHub OAuth tokens or Bedrock model API keys in code.

The implementer container has outbound network access (`Bash` / `WebFetch` / `WebSearch`). Container credentials are scoped (Bedrock + project S3 + GitHub App for the target repo) and the only path code reaches the repo is a human-reviewed PR — that's the load-bearing control, not egress filtering.
