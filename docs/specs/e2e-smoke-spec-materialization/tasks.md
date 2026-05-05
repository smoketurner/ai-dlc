# Tasks — End-to-End Smoke Test After Spec Materialization Fix

> **Spec slug:** `e2e-smoke-spec-materialization`

Ordered, atomic units. Each task is one PR.

- [ ] **T-001** — Create pre-canned SpecBundle fixture JSON
  - **Implements:** AC-001
  - **Touches:** `tests/e2e/__init__.py`, `tests/e2e/fixtures/smoke_spec_bundle.json`
  - **Done when:** tests/e2e/fixtures/smoke_spec_bundle.json exists, contains a minimal valid SpecBundle, and passes schema validation when loaded in a unit test.

- [ ] **T-002** — Implement e2e smoke test for spec materialization
  - **Implements:** AC-001, AC-002, AC-003
  - **Touches:** `tests/e2e/test_spec_materialization_smoke.py`
  - **Done when:** tests/e2e/test_spec_materialization_smoke.py exists with tests that: (1) load and validate the fixture against the SpecBundle schema, (2) invoke materialization into tmp_path, (3) assert requirements.md, design.md, tasks.md exist and are non-empty, (4) assert expected section headers are present. All tests pass locally via `pytest tests/e2e/`.

- [x] **T-003** — Add e2e smoke test job to CI workflow
  - **Implements:** AC-004
  - **Touches:** `.github/workflows/ci.yml`
  - **Done when:** .github/workflows/ci.yml contains a job that runs `pytest tests/e2e/` on push to main and on PRs, and the job succeeds in CI.
