# Requirements — End-to-End Smoke Test After Spec Materialization Fix

> **Spec slug:** `e2e-smoke-spec-materialization`

## Summary

Add an end-to-end smoke test that exercises the full spec materialization pipeline — from user intent submission through SpecBundle generation, Markdown file creation, and PR opening — to prevent regressions in the spec materialization flow.

## User stories

- **R-001** — As a developer, I want run a single smoke test that validates the entire spec materialization pipeline end-to-end so that I can catch regressions in spec materialization before they reach production.
- **R-002** — As a CI pipeline, I want automatically execute the end-to-end smoke test on every push to main and on PRs so that broken spec materialization is detected before merge.

## Acceptance criteria

- **AC-001** (R-001) — Given the smoke test is invoked with a minimal valid user intent, when the spec materialization pipeline processes the intent, then a SpecBundle JSON object conforming to the schema is produced without errors.
- **AC-002** (R-001) — Given a valid SpecBundle has been produced, when the materialization step writes Markdown files, then requirements.md, design.md, and tasks.md are created under docs/specs/{spec_slug}/.
- **AC-003** (R-001) — Given the Markdown files have been written, when the smoke test validates their content, then each file is non-empty and contains expected section headers (e.g., '## User Stories' in requirements.md, '## Components' in design.md, '## Tasks' in tasks.md).
- **AC-004** (R-002) — Given a push to main or a PR is opened, when the CI workflow runs, then the e2e smoke test job executes and reports pass/fail status.

## Out of scope

- Testing the actual GitHub PR creation API call (mocked at boundary)
- Load or performance testing of the pipeline
- Testing LLM response quality or content correctness beyond schema conformance

## Open questions

- Is there an existing test fixture or mock for the Architect agent's LLM call, or should the smoke test use a pre-canned SpecBundle JSON fixture to bypass the LLM?
- Should the smoke test run against a temporary directory or use a dedicated test output path?
