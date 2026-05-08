# Project Memory

This file is the human-reviewed source of truth for project-scoped context that ai-dlc agents read at the start of every session and propose updates to via PR. Cross-session semantic facts (per-user preferences, team-level signals) live in AgentCore Memory, not here.

Six sections, in order. Agents fail-fast on unknown headers.

## Overview

ai-dlc is the agentic SDLC platform itself. Seven agents (Architect, Critic, Implementer, Reviewer, Tester, Triage, Proposer) drive a spec-driven pipeline gated by GitHub PR reviews. The Architect writes a spec (requirements + design + tasks); the Critic advises on it; the spec PR is reviewed and merged as a bundle; the Implementer then works through `tasks.md` opening one PR per task with the Reviewer and Tester acting as advisors. Triage classifies issue-driven runs; Proposer drives memory and prompt updates. Orchestration is an SQS-beacon + DDB-state machine driven by a single `state_router` Lambda.

## Conventions

- Python 3.14, Astral toolchain only (`uv`, `ruff`, `ty`).
- All agents ship as `linux/arm64` container images on AgentCore Runtime.
- Pin every dependency to an exact version. Pin every GitHub Action to a SHA with a version comment.
- Replace, don't deprecate: when a new implementation supersedes an old one, remove the old one entirely.
- **Spec-driven**: every feature ships as a three-document spec under `docs/specs/{slug}/{requirements,design,tasks}.md`. The Architect writes the spec; reviewers approve it as a bundle (one HITL gate). The Implementer works the `tasks.md` checklist, opening **one PR per task**.
- Markdown everywhere: requirements, design, tasks, ADRs, MEMORY.md.

## Decisions

Two kinds of decisions, both linked from here.

**Specs** — one per feature, three documents per spec:

```
docs/specs/{slug}/
  requirements.md   — user stories + acceptance criteria
  design.md         — how it's built; data model, components, sequence
  tasks.md          — ordered, atomic units (`- [ ] T-001 ...`); each links to a requirement
```

Format the bullet here as: `- [{slug}](docs/specs/{slug}/): one-line summary`.

**ADRs** — cross-cutting architectural decisions that outlive a single spec. Most specs don't produce an ADR; one is added when the design surfaces a decision worth committing to long-term. Format: `- [ADR-NNNN](docs/ADRs/NNNN-slug.md): one-line summary`.

## Constraints

- AgentCore Runtime allows only Python 3.10–3.13 in `code_configuration`; we use `container_configuration` with our own 3.14 image.
- AgentCore Runtime requires `linux/arm64` images.

## Glossary

- **Spec** — A three-document feature bundle (`requirements.md`, `design.md`, `tasks.md`) under `docs/specs/{slug}/`. Written by the Architect, approved as a unit, executed task-by-task by the Implementer.
- **Task** — One checkbox in a spec's `tasks.md`. Atomic, links back to a requirement, gets its own PR and HITL gate.
- **ADR** — Architectural Decision Record. Cross-cutting decision under `docs/ADRs/`. Surfaces from a spec's design when something is worth committing to long-term.
- **HITL** — Human-in-the-loop. Mandatory PR-review gates: one for the spec, one per task.
- **Run** — One execution of the SDLC pipeline. Identified by a UUID7 `run_id`.

## Notes

(Free-form. Append-only. The Implementer pushes incidental observations here when they don't yet rise to an ADR.)
