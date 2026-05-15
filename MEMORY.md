# Project Memory

This file is the human-reviewed source of truth for project-scoped context that ai-dlc agents read at the start of every session and propose updates to via PR. Cross-session semantic facts (per-user preferences, team-level signals) live in AgentCore Memory, not here.

Six sections, in order. Agents fail-fast on unknown headers.

## Overview

ai-dlc is the agentic SDLC platform itself. Nine agents (Architect, Critic, Implementer, Reviewer, Tester, Code-Critic, Triage, Proposer, Retrospector) drive a single-PR-per-issue pipeline gated by GitHub PR review + GitHub Checks. Triage classifies an issue-driven run; the Architect writes a structured `plan.md` to S3 (Context, Assumptions, Approach, Files, Reuse, Implementation steps, Verification, Out of scope); the Critic adversarially reviews the plan; the Implementer opens a single impl PR for the run. Reviewer, Tester, and Code-Critic then run in parallel against the PR — the Code-Critic specifically reviews how well the implementation addresses the original GitHub issue. Humans steer via `@aidlc-bot` mentions on the PR; the Implementer auto-iterates on reviewer-requested changes and failing GitHub Checks (capped at three automated revisions). Proposer drives memory/prompt update PRs; Retrospector fires on terminal events. Orchestration is an SQS-beacon + DDB state machine driven by a single `state_router` Lambda.

## Conventions

- Python 3.14, Astral toolchain only (`uv`, `ruff`, `ty`).
- All agents ship as `linux/arm64` container images on AgentCore Runtime.
- Pin every dependency to an exact version. Pin every GitHub Action to a SHA with a version comment.
- Replace, don't deprecate: when a new implementation supersedes an old one, remove the old one entirely.
- **Single-PR-per-issue**: each GitHub issue assigned to the bot produces exactly one impl PR. The Architect's `plan.md` is an internal S3 artifact — not committed to git. The Implementer reads it, executes on one branch (`aidlc/impl/{run_id}`), opens one PR. Reviewer + Tester + Code-Critic then run in parallel against that PR; their markdown outputs land in S3 and as PR comments.
- Markdown everywhere: plan, validator outputs, ADRs, MEMORY.md.
- Every Python Lambda depends on `aws-lambda-powertools==3.28.0` and uses its `Logger` / `Tracer` / `Metrics` / `event_source` primitives in place of stdlib `logging`.
- Prefer `terraform-aws-modules/*/aws` community modules over bespoke wrappers for AWS resources. Bespoke code only where the community module doesn't cover the surface (e.g., project-specific security-group rules, the EventBridge schema registry).
- In every Terraform module: `data` blocks live in `data.tf`, `locals` in `locals.tf`. Resource files (`lambdas.tf`, `sqs.tf`, etc.) contain only `resource` blocks.
- Don't underscore-prefix module-private names by default. Plain names everywhere. Reach for `_` only when it carries real information (`_unused_arg`, shadow-avoidance).
- Don't reference roadmap phase numbers ("Phase 11a", "12c") in code, docstrings, prompts, or PR bodies. Name the mechanism, not the planning bucket.
- No speculative "future work" comments ("a later iteration could…", "for now"). Either delete the line or convert it to `@TODO` with a concrete next step.
- Agent web-fetch tools never cache. Freshness is the whole point.

## Decisions

**ADRs** — cross-cutting architectural decisions worth committing to long-term. The Architect's `plan.md` lives in S3 (`runs/{run_id}/plan.md`), not in the repo; if a plan surfaces an architectural choice with multi-run reach, the Implementer commits a new ADR under `docs/ADRs/` as part of the impl PR. Format: `- [ADR-NNNN](docs/ADRs/NNNN-slug.md): one-line summary`.

**Standing decisions** — load-bearing choices that aren't tied to a single spec:

- **API Gateway + Lambda** for the dashboard and other request/response workloads. We switched off ECS Fargate behind an ALB for cost reasons — pay-per-request beats an always-on Fargate task at our request volume. Cognito OIDC sits in front of API Gateway.
- **FastAPI + Jinja2 + Alpine.js (CDN, no JS build step)** for the dashboard and any future internal UI. Not React/TypeScript SPAs — language uniformity and zero JS toolchain are the goal. Reach for SSE for live updates; WebSocket only when truly bidirectional.

## Constraints

- AgentCore Runtime allows only Python 3.10–3.13 in `code_configuration`; we use `container_configuration` with our own 3.14 image.
- AgentCore Runtime requires `linux/arm64` images.

## Glossary

- **Plan** — The single markdown document the Architect writes for a run (`s3://artifacts/runs/{run_id}/plan.md`). Sections: Context, Assumptions, Approach, Files, Reuse, Implementation steps, Verification, Out of scope. Internal artifact — not committed to git.
- **Review** — The Reviewer's code-review markdown (`s3://artifacts/runs/{run_id}/validation/review-r{N}.md`). Verdict (`approve`/`request_changes`/`comment`) gates the run. Includes per-comment severity counts and `assumption_check` entries that verify each architect assumption against the source issue.
- **Test report** — The Tester's gap analysis (`s3://artifacts/runs/{run_id}/validation/test_report-r{N}.md`). Advisory. Leads with an `existing_tests` enumeration before listing gaps.
- **Code-critique** — The Code-Critic's adversarial review of the impl PR against the **original GitHub issue** (`s3://artifacts/runs/{run_id}/validation/critique-r{N}.md`). Severity-tagged findings tagged by lens (`issue→diff`, `user-problem`, `plan-drift`, `edge-case`). Advisory — doesn't gate the run.
- **Impl PR** — The single GitHub PR opened by the Implementer for a run, off branch `aidlc/impl/{run_id}`. The only PR a human reviews per run.
- **ADR** — Architectural Decision Record. Cross-cutting decision under `docs/ADRs/`. Surfaced from a plan when a choice has multi-run reach; committed as part of the impl PR.
- **HITL** — Human-in-the-loop. PR review on the impl PR (one gate). Additionally, humans steer mid-run by `@aidlc-bot` mentions on the impl PR.
- **Revision** — One implementer pass after the initial PR opens. Triggered by a reviewer requesting changes, a `@aidlc-bot` mention, or a failing GitHub Check. Automated revisions are capped at three per run; human-mention revisions are uncapped.
- **Run** — One execution of the SDLC pipeline. Identified by a UUID7 `run_id`.

## Notes

(Free-form. Append-only. The Implementer pushes incidental observations here when they don't yet rise to an ADR.)
