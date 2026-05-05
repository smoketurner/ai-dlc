"""Re-export :class:`TriageDecision` under the agent's namespace.

The decision model lives under :mod:`common.triage` so the webhook
Lambda can validate triage responses without depending on the agent
package. Consumers that want a stable agent-scoped import path use
``from triage.decision import TriageDecision``.
"""

from __future__ import annotations

from common.triage import MissingInformation, TriageAction, TriageDecision, WorkflowKind

__all__ = ["MissingInformation", "TriageAction", "TriageDecision", "WorkflowKind"]
