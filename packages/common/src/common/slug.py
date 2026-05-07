"""Project-slug helpers shared by every entry path.

The dashboard, the entry_adapter Lambda, and the GitHub-webhook handler
must agree on the slug format so PRs / events / DDB rows for the same
target repo can be cross-referenced. Keeping the single derivation here
prevents drift.
"""

from __future__ import annotations


def slug_from_repo(target_repo: str) -> str:
    """``owner/name`` → ``owner-name`` (lowercase). Stable across entry paths.

    Used by the dashboard's ``POST /v1/runs``, the GitHub webhook's
    triage trigger, and any other surface that writes ``project_slug``
    onto a run STATE row.
    """
    return target_repo.lower().replace("/", "-")
