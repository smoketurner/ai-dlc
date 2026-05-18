# Development Conventions

Project-wide conventions for code, tooling, and style.

## Language & Toolchain

- **Python 3.14** with the Astral toolchain only: `uv` (workspace manager), `ruff` (linter/formatter), `ty` (type checker).
- `uv` workspace: each agent, lambda, and package is a workspace member under a root `pyproject.toml`.
- All agents ship as `linux/arm64` container images.

## Dependency Management

- Pin every dependency to an exact version (no ranges, no `>=`).
- Pin every GitHub Action to a SHA with a version comment (e.g., `uses: actions/checkout@<sha> # v6.0.2`).

## Code Philosophy

- **Replace, don't deprecate**: when a new implementation supersedes an old one, remove the old one entirely.
- **Markdown everywhere**: plans, validator outputs, ADRs, MEMORY.md.
- **No underscore-prefix** by default. Plain names everywhere. Reach for `_` only when it carries real information (`_unused_arg`, shadow-avoidance).
- **No roadmap phase numbers** in code, docstrings, prompts, or PR bodies. Name the mechanism, not the planning bucket.
- **No speculative future-work comments** ("a later iteration could...", "for now"). Either delete the line or convert it to `@TODO` with a concrete next step.
- **Agent web-fetch tools never cache**. Freshness is the whole point.

## AWS Lambda Standards

Every Python Lambda depends on `aws-lambda-powertools` and uses its primitives:
- `Logger` (not stdlib `logging`)
- `Tracer` (X-Ray tracing)
- `Metrics` (CloudWatch metrics)
- `event_source` decorators

## Terraform Conventions

- Prefer `terraform-aws-modules/*/aws` community modules over bespoke wrappers.
- Bespoke code only where the community module doesn't cover the surface.
- File layout within each module:
  - `data.tf` -- `data` blocks only
  - `locals.tf` -- `locals` blocks only
  - Resource files (`lambdas.tf`, `sqs.tf`, etc.) -- `resource` blocks only
  - `variables.tf`, `outputs.tf`, `versions.tf` as standard

## Testing

```bash
uv run pytest -q                           # unit tests only
uv run pytest -m integration               # moto-backed integration tests
uv run pytest -m live_aws tests/integration/...  # full e2e against dev account (gated)
```

## Linting & Type Checking

```bash
uv run ruff check                          # lint
uv run ty check                            # type check
```

## Code Style

- Max 100 lines per function
- Cyclomatic complexity max 8
- Max 5 positional parameters
- 100-character line width
- Absolute imports only
- Google-style docstrings
- Zero warnings policy (no suppressed warnings without justification)

## Implementer Guardrails (Deny-List)

The Implementer agent container enforces a deny-list via hooks. Blocked operations:

- `rm -rf /`, `rm -rf $HOME`, `chmod -R 777`
- `git push --force-with-lease origin main`
- `aws iam *Delete*`
- `terraform apply` against `prod`
- `kubectl delete`
- `dropdb` / `DROP TABLE`
- Direct GitHub OAuth tokens or Bedrock model API keys in code

The Implementer has outbound network access (Bash/WebFetch/WebSearch). Container credentials are scoped (Bedrock + project S3 + GitHub App for the target repo). The load-bearing security control is the human-reviewed PR, not egress filtering.

## A/B Prompt Routing

Each agent has a `prompts.py` with a `SYSTEM_PROMPT`. To A/B test a prompt rewrite:

1. Add `prompts_b.py` alongside `prompts.py` with a `SYSTEM_PROMPT` string.
2. `routing.pick_variant(run_id, agent_name)` deterministically selects `"a"` or `"b"` per run+agent.
3. The variant tag flows through `actor_id` on every event (e.g., `architect-b`), enabling metric splits.
4. Removing `prompts_b.py` silently falls back to the A variant -- no plumbing changes needed.
