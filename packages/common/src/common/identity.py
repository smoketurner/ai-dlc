"""Identity derivation for AgentCore Runtime invocations.

When the state-router (or retrospector-dispatcher) calls
``invoke_agent_runtime``, AgentCore mints the agent's workload access
token on behalf of the ``runtimeUserId`` passed on the request. The
container's :class:`bedrock_agentcore.runtime.BedrockAgentCoreApp`
reads the resulting ``WorkloadAccessToken`` header and pushes it into
:class:`BedrockAgentCoreContext` so the SDK's
``@requires_access_token`` decorator can exchange it for downstream
M2M / OAuth tokens. Without a ``runtimeUserId`` the SDK falls through
to local-dev auth, which tries to call ``CreateWorkloadIdentity`` and
fails closed under the runtime's least-privilege role.

The identity precedence is:

1. ``cognito:{requestor_sub}`` — preferred when the run was minted
   from a dashboard submission (the requester authenticated through
   Cognito).
2. ``gh-app:{login}`` — when the GitHub login carries a ``[bot]``
   suffix (GitHub App installation token). Brackets are preserved
   verbatim; they're valid HTTP header VCHAR per RFC 7230 and the
   AgentCore API accepts the unmangled value.
3. ``gh:{login}`` — otherwise a human GitHub login.
4. ``system:{actor_id}`` fallback — should only fire if a malformed
   trigger arrives without identity context.
"""

from __future__ import annotations

DEFAULT_FALLBACK = "system:unknown"


def runtime_user_id(
    *,
    requestor_sub: str | None = None,
    requestor: str | None = None,
    fallback: str = DEFAULT_FALLBACK,
) -> str:
    """Derive the ``runtimeUserId`` for an ``invoke_agent_runtime`` call.

    Args:
        requestor_sub: Cognito ``sub`` of the human who submitted the
            request via the dashboard. ``None`` for GitHub-driven runs.
        requestor: The GitHub login (or other actor string) that
            originated the request. May include a ``[bot]`` suffix
            for App installation tokens.
        fallback: Identity to return when neither ``requestor_sub`` nor
            ``requestor`` carries usable data. The caller can pass a
            more specific marker (e.g. ``"system:retrospector"``) than
            the module default.

    Returns:
        A namespaced identity string suitable for the AgentCore
        ``runtimeUserId`` parameter. Always non-empty.
    """
    if requestor_sub:
        return f"cognito:{requestor_sub}"
    if requestor:
        stripped = requestor.strip()
        if stripped:
            namespace = "gh-app" if stripped.endswith("[bot]") else "gh"
            return f"{namespace}:{stripped}"
    return fallback


def revision_commenter(pending_revision_feedback: list[dict[str, object]]) -> str | None:
    """Return the most recent human commenter from the revision-feedback queue.

    ``pending_revision_feedback`` is the append-ordered list the
    projector accumulates on the run's STATE row. Each entry is one
    :data:`common.runtime.FeedbackItem`. Items with ``kind="ci_failure"``
    have no human attribution and are skipped.

    Args:
        pending_revision_feedback: The list as read off the STATE row.

    Returns:
        The GitHub login of the latest human-driven feedback item, or
        ``None`` when the queue is empty or only contains CI failures.
        Callers pass the result through :func:`runtime_user_id` to
        namespace it; ``None`` means fall back to the original
        ``run.requestor``.
    """
    for item in reversed(pending_revision_feedback):
        kind = item.get("kind")
        if kind == "review_changes_requested":
            reviewer = item.get("reviewer")
            if isinstance(reviewer, str) and reviewer:
                return reviewer
        elif kind in ("review_comment_mention", "issue_comment_mention"):
            commenter = item.get("commenter")
            if isinstance(commenter, str) and commenter:
                return commenter
    return None
