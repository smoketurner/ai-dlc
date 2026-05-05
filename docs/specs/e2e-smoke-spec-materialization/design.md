# Design — End-to-End Smoke Test After Spec Materialization Fix

> **Spec slug:** `e2e-smoke-spec-materialization`

## Approach

Introduce a pytest-based end-to-end smoke test module that exercises the spec materialization pipeline. The test uses a pre-canned SpecBundle fixture (valid JSON) to decouple from LLM availability, feeds it through the materialization logic, and asserts that the expected Markdown files are produced with correct structure. A new CI workflow job runs this test on every push to main and on PRs.

## Components

- **E2E Smoke Test Module** (`tests/e2e/test_spec_materialization_smoke.py`) — Pytest module containing the end-to-end smoke test that validates spec materialization from SpecBundle input through Markdown file output.
- **SpecBundle Fixture** (`tests/e2e/fixtures/smoke_spec_bundle.json`) — A pre-canned valid SpecBundle JSON file used as deterministic input for the smoke test, avoiding LLM dependency.
- **CI Workflow Update** (`.github/workflows/ci.yml`) — Add an e2e smoke test job to the existing CI workflow so the test runs automatically on push to main and on PRs.

## Data model

```text
The test operates on the existing SpecBundle schema (defined in the project). The fixture file contains a minimal but complete SpecBundle JSON object with:
- spec_slug: "smoke-test-fixture"
- feature_name: "Smoke Test Fixture Feature"
- requirements: summary, one user story, one acceptance criterion
- design: approach, one component, data_model, sequence
- tasks: one task implementing the single acceptance criterion

No new persistent data models are introduced.
```

## Sequence

```text
1. CI triggers on push/PR.
2. pytest discovers tests/e2e/test_spec_materialization_smoke.py.
3. Test loads tests/e2e/fixtures/smoke_spec_bundle.json and validates it against the SpecBundle schema (AC-001).
4. Test invokes the materialization function with the parsed SpecBundle and a temporary output directory (via tmp_path fixture).
5. Materialization writes requirements.md, design.md, tasks.md into tmp_path/docs/specs/smoke-test-fixture/.
6. Test asserts all three files exist and are non-empty (AC-002).
7. Test asserts expected section headers are present in each file (AC-003).
8. CI job reports pass/fail (AC-004).
```

## Failure modes & mitigations

- If the materialization function signature changes, the smoke test will fail — update the test accordingly.
- If the SpecBundle schema evolves (new required fields), the fixture JSON must be updated to remain valid.

## Trade-offs

- Using a pre-canned fixture means we do not test the Architect agent's LLM integration in this smoke test — but this keeps the test deterministic and fast.
- Adding a new CI job increases pipeline time slightly but catches regressions early.
