# Adopting ai-dlc on your repo

This guide is for project maintainers who want the ai-dlc agentic team
(Architect, Implementer, Reviewer, Tester, Code-Critic, Triage,
Proposer, Retrospector) to operate on a GitHub repo they own.

ai-dlc is a *platform* — one deployment serves many repos. You don't
fork it for each project; you install its GitHub App on the repo,
optionally add a `MEMORY.md` so the agents know your conventions, and
submit runs via the dashboard or API.

## Prerequisites

- A GitHub repo you administer.
- Access to an ai-dlc dashboard or API endpoint (your organization's
  operator runs the platform; ask them for the URL).
- The ai-dlc GitHub App installed on the target repo (the operator
  shares the install URL).

## Step 1 — Install the GitHub App

Open the install URL the operator gave you and pick the repo(s) to
authorize. The app needs `contents: read/write`, `pull_requests:
read/write`, `issues: read/write`, and `checks: read` so it can clone,
open PRs, comment, and react to webhooks.

Once installed, the dashboard's repo picker will list your repo.

## Step 2 — Add a `MEMORY.md` (recommended)

Drop a `MEMORY.md` at the root of your repo. The Architect reads it
first and applies its rules across the run. The file uses six fixed
sections in this order:

```markdown
# Project Memory

One-paragraph summary of what the project is.

## Overview
What the project does, who uses it, what its goals are.

## Conventions
Toolchain, dependency-pinning, naming, formatting, style rules.

## Decisions
ADR-style bullets — irreversible-ish choices and their why.

## Constraints
Hard limits (runtime versions, hardware, compliance, etc.).

## Glossary
Domain terms the agents need to use correctly.

## Notes
Anything else worth flagging.
```

Keep it short — ai-dlc's own `MEMORY.md` ([here](MEMORY.md)) is the
canonical example. The platform won't reject a missing `MEMORY.md`, but
the Architect will infer conventions from the file tree, which is
slower and less reliable.

`AGENTS.md` (also at the root) is the companion file for agent-specific
guidance — e.g., directories the agents shouldn't touch, sensitive
paths, where ADRs live.

Legacy projects with `docs/MEMORY.md` are still supported; the platform
prefers the root location when both exist.

## Step 3 — Author a stack profile (automatic)

The platform auto-detects your stack — languages, package managers,
test/build/lint commands, workspace kind — on every Architect run. You
don't write this; it's computed from your manifests
(`pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod` /
`pom.xml` / etc.), your `Makefile`, and your GitHub Actions workflows.

If detection misses something or picks the wrong default, document the
correct command in `MEMORY.md` under "Conventions" — that wins over
auto-detection.

## Step 4 — Multi-language sandbox setup (optional)

The Reviewer and Tester run your test suite inside an Amazon Bedrock
AgentCore Code Interpreter sandbox. That sandbox ships with Python,
Node.js, and TypeScript. For any other language, drop a bootstrap
script at `.aidlc/sandbox-bootstrap.sh`:

```bash
mkdir -p .aidlc
cp docs/sandbox-bootstrap-templates/rust.sh .aidlc/sandbox-bootstrap.sh
git add .aidlc/sandbox-bootstrap.sh && git commit -m "ai-dlc: rust sandbox bootstrap"
```

Templates ship for Rust, Go, Java, and Ruby in
[`docs/sandbox-bootstrap-templates/`](docs/sandbox-bootstrap-templates/).
Polyglot repos can concatenate the install steps in a single script.

The Implementer runs in a separate AgentCore Runtime container that
already includes [`mise`](https://mise.jdx.dev) for installing extra
toolchains on demand — it doesn't need the sandbox bootstrap.

## Step 5 — Submit a run

Two paths:

**Dashboard.** Pick your repo from the dropdown, type the request
(e.g., "Add a `/healthz` endpoint that returns 200 OK"), submit. The
dashboard shows the run state machine in real time.

**API.** `POST /v1/runs` with:

```json
{
  "target_repo": "owner/repo",
  "intent": "Add a /healthz endpoint that returns 200 OK"
}
```

Either way you'll see a single impl PR within a few minutes. It's the
only PR you need to review — merge gates the run.

## What happens automatically

- **Plan:** Architect reads the issue + your `MEMORY.md` / `AGENTS.md`
  and writes a single `plan.md` to S3 (Context, Assumptions, Approach,
  Files, Reuse, Implementation steps, Verification, Out of scope).
  Internal artifact; no spec PR.
- **Implementation:** One Implementer PR off `aidlc/impl/{run_id}`.
- **Validation:** As soon as the impl PR opens, Reviewer + Tester +
  Code-Critic run in parallel against it. The Reviewer gates the run
  (`approve` / `comment` / `request_changes`) and verifies each
  architect assumption against your original issue text. The Tester
  reports test gaps. The Code-Critic grades the PR against the
  original issue. All three post a bot comment on the PR.
- **Revisions:** A `@aidlc-bot` mention on the PR, a `request_changes`
  reviewer verdict, or a failing required CI Check kicks off an
  implementer revision pass on the same branch.
- **Retrospectives:** On every terminal event (PR merge/close,
  issue close), the Retrospector reads the artifacts and may propose a
  `MEMORY.md` or `AGENTS.md` edit as a PR.
- **Research:** Issues tagged for research route to the Proposer, which
  reads external docs and proposes prompt or memory edits as a PR.

## Troubleshooting

- **The Architect's design references the wrong stack.** Check the
  detected stack profile by inspecting `s3://<memory-bucket>/projects/<slug>/stack_profile.json`
  (operator helps). Fix by adding explicit commands under
  "Conventions" in `MEMORY.md`.
- **Tests fail with "command not found".** The sandbox needs a
  bootstrap script for non-Python/JS stacks — see Step 4.
- **A run hangs in `tasks_in_progress`.** Look at the dashboard's run
  detail — usually a PR is waiting for human approval at a task gate.

## Limits

- One platform deployment serves one logical "organization" of repos
  today; per-org multi-tenancy is on the roadmap, not shipped.
- The GitHub App must be installed per repo. There is no dashboard
  wizard for this yet — use the operator's install URL.
- The Code Interpreter sandbox session times out around 600s. Slow
  test suites need to bring their toolchain warm via the bootstrap
  script or split into smaller suites.
