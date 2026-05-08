# Design — {feature name}

> **Spec slug:** `{slug}` &nbsp;·&nbsp; **Status:** draft

## Approach

How we solve the requirements. One or two paragraphs at the top — pick the option, then justify.

## Components

- **{Component A}** — purpose, where it lives in the repo, key types.
- **{Component B}** — …

## Data model

```text
{schema sketch — pydantic, DDB key shape, S3 key layout, etc.}
```

## Sequence

```text
{numbered steps describing the happy path; reference component names above}
```

## Testing strategy

How this spec is verified. Map each acceptance criterion to a test kind (unit / integration / property / e2e), name the test file(s), and call out fixtures, mocks, or environment requirements. The Tester agent reads this section to flag coverage gaps.

## Failure modes & mitigations

- **{What can break}** — how we detect it, what we do.

## Trade-offs

- **{Decision}** — chose X over Y because …

## ADRs proposed

If this design surfaces a cross-cutting decision worth recording beyond the spec, list the ADR(s) here. Most designs add nothing here.

- {`docs/ADRs/NNNN-slug.md` — one-line summary}

## References

- {requirements.md sections this design implements}
- {prior specs / ADRs / external docs}
