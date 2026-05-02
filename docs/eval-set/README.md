# Eval set

Ten representative SDLC tasks covering the breadth of work the platform is expected to handle. Each case has the same shape:

```text
docs/eval-set/{slug}.md
  - Title + slug
  - Intent: the prompt a human would submit via the dashboard
  - Setup: any pre-existing project state the case assumes
  - Expected behaviour: what the architect's spec should produce, and how the
    implementer should turn the tasks into PRs
  - Pass criteria: testable assertions for the run + PRs
```

These cases drive two things:

1. **Manual smoke testing** during development — a human runs each through the live pipeline and confirms the pass criteria.
2. **AgentCore Evaluations** — when AgentCore Evaluations becomes GA we wire these as the eval suite; until then they're documentation + manual fixtures.

Pass criteria are deliberately observable (cost cap, PR count, files touched, presence of acceptance-criteria coverage in `requirements.md`) — not based on prose comparisons against a golden spec. The architect's spec text is allowed to vary; the structural and behavioural outcomes are not.
