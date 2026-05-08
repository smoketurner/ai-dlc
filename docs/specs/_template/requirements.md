# Requirements — {feature name}

> **Spec slug:** `{slug}` &nbsp;·&nbsp; **Status:** draft &nbsp;·&nbsp; **Owner:** {agent or person}

## Summary

One paragraph: what we're building and why a user cares.

## User stories

- **R-001** — As a {role}, I want {capability} so that {outcome}.
- **R-002** — …

## Acceptance criteria

Each criterion uses [EARS notation](https://alistairmavin.com/ears/) and traces to a user story. Pick the most specific pattern; describe **system behaviour**, not test infrastructure.

- **AC-001** (R-001) — WHEN {trigger}, THE SYSTEM SHALL {observable response}.
- **AC-002** (R-001) — WHILE {state}, THE SYSTEM SHALL {observable response}.
- **AC-003** (R-002) — IF {error condition}, THEN THE SYSTEM SHALL {observable response}.
- **AC-004** (R-002) — WHERE {feature flag is enabled}, THE SYSTEM SHALL {observable response}.
- **AC-005** (R-002) — THE SYSTEM SHALL {invariant — always true}.

Patterns may combine — most commonly a `WHILE` clause prefixed onto an event:

- **AC-006** (R-003) — WHILE {state}, WHEN {trigger}, THE SYSTEM SHALL {observable response}.

## Out of scope

- {Things explicitly not covered by this spec.}

## Open questions

- {Anything the reviewer needs to decide before approval.}
