"""Shared persona snippets composed by individual agent prompts.

Centralises a few short text constants that recur across agent prompts
so they stay in sync — the door taxonomy, MEMORY.md discipline language,
the PR-prose vocabulary ban, and a small helper to render a
coordination footer. Each agent's ``prompts.py`` uses these via f-string
composition or direct quotation; nothing here imports any agent module.
"""

from __future__ import annotations

DOOR_TAXONOMY: str = """\
The ten one-way door categories — irreversible without significant cost:

- ``schema_migration`` — DB schema change (ALTER/DROP/type change).
- ``public_api_break`` — removed/renamed public export, HTTP route, or
  event-payload field already in a producer→consumer contract.
- ``production_terraform`` — any change under ``terraform/envs/prod/``.
- ``iam_authorization`` — IAM roles/policies, Cedar policies, KMS key
  policies, gateway target ACLs.
- ``auth_flow`` — Cognito user pool config, OIDC callback, token handling.
- ``cryptography_or_secrets`` — KMS keys, encryption-at-rest mode change,
  secret-rotation logic.
- ``major_dependency_bump`` — semver-major change in any pinned dep.
- ``scheduled_job`` — EventBridge schedule / cron / Lambda
  EventSourceMapping with a polling cadence.
- ``event_schema_breaking`` — non-additive change to an event schema in
  use by a consumer.
- ``public_deletion`` — deletion of a published file/module/function
  whose symbol appears in another file.
"""

MEMORY_MD_DISCIPLINE: str = """\
Read ``docs/MEMORY.md`` (Conventions section) before acting. The file
encodes project-scoped rules — Astral toolchain, ARM64 containers,
exact-pinned deps, terraform-aws-modules where they fit, no
underscore-prefixed names, ``aws-lambda-powertools`` 3.28.0 for any
Lambda. Apply the conventions you find there; if a rule conflicts with
what you'd otherwise do, the file wins.
"""

PR_PROSE_VOCABULARY_BAN: str = """\
PR-prose discipline. When you write a PR description, commit subject, or
issue comment, use plain factual language. A bug fix is a bug fix, not a
"critical stability improvement." Avoid these words: ``critical``,
``crucial``, ``essential``, ``significant``, ``comprehensive``,
``robust``, ``elegant``. Describe what the code does now — not what it
used to do, not what was discarded along the way.
"""


def coordination_footer(
    *,
    role: str,
    predecessor: str,
    expected_context: str,
    focus: str,
) -> str:
    """Render a short coordination block for an agent's system prompt.

    Args:
        role: Name of this agent ("Architect", "Reviewer", ...).
        predecessor: Agent or trigger that runs immediately before this
            one.
        expected_context: One-sentence description of what should be in
            the input by the time this agent runs.
        focus: One-sentence description of what this agent contributes
            that the predecessor did not.

    Returns:
        A plain-text block ready to drop into a system prompt.
    """
    return (
        f"Coordination ({role}):\n"
        f"  - Predecessor: {predecessor}\n"
        f"  - Expected context: {expected_context}\n"
        f"  - Focus: {focus}\n"
    )


__all__ = [
    "DOOR_TAXONOMY",
    "MEMORY_MD_DISCIPLINE",
    "PR_PROSE_VOCABULARY_BAN",
    "coordination_footer",
]
