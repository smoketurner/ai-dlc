# Gates blocked

Last failing command: `make test`

## Output

```
/usr/local/bin/uv run pytest -q -m "not integration and not live_aws and not eval"

==================================== ERRORS ====================================
__________ ERROR collecting packages/common/tests/test_browse_url.py ___________
ImportError while importing test module '/workspace/repo/packages/common/tests/test_browse_url.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
packages/common/tests/test_browse_url.py:13: in <module>
    import playwright.sync_api as pw
E   ModuleNotFoundError: No module named 'playwright'
____________ ERROR collecting agents/architect/tests/test_agent.py _____________
ImportError while importing test module '/workspace/repo/agents/architect/tests/test_agent.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/architect/tests/test_agent.py:7: in <module>
    from architect.agent import compose_message
E   ModuleNotFoundError: No module named 'architect'
__________ ERROR collecting agents/architect/tests/test_app_async.py ___________
ImportError while importing test module '/workspace/repo/agents/architect/tests/test_app_async.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/architect/tests/test_app_async.py:18: in <module>
    from architect import app
E   ModuleNotFoundError: No module named 'architect'
___________ ERROR collecting agents/architect/tests/test_app_emit.py ___________
ImportError while importing test module '/workspace/repo/agents/architect/tests/test_app_emit.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/architect/tests/test_app_emit.py:9: in <module>
    from architect import app
E   ModuleNotFoundError: No module named 'architect'
___________ ERROR collecting agents/architect/tests/test_app_run.py ____________
ImportError while importing test module '/workspace/repo/agents/architect/tests/test_app_run.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/architect/tests/test_app_run.py:19: in <module>
    from architect import app
E   ModuleNotFoundError: No module named 'architect'
____________ ERROR collecting agents/architect/tests/test_hooks.py _____________
ImportError while importing test module '/workspace/repo/agents/architect/tests/test_hooks.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/architect/tests/test_hooks.py:8: in <module>
    from architect.hooks import build_hooks
E   ModuleNotFoundError: No module named 'architect'
_____________ ERROR collecting agents/architect/tests/test_plan.py _____________
ImportError while importing test module '/workspace/repo/agents/architect/tests/test_plan.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/architect/tests/test_plan.py:5: in <module>
    from architect.plan import (
E   ModuleNotFoundError: No module named 'architect'
________ ERROR collecting agents/architect/tests/test_repo_grounding.py ________
ImportError while importing test module '/workspace/repo/agents/architect/tests/test_repo_grounding.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/architect/tests/test_repo_grounding.py:13: in <module>
    from architect import repo_grounding
E   ModuleNotFoundError: No module named 'architect'
_________ ERROR collecting agents/code_critic/tests/test_app_async.py __________
ImportError while importing test module '/workspace/repo/agents/code_critic/tests/test_app_async.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/code_critic/tests/test_app_async.py:17: in <module>
    from code_critic import app
E   ModuleNotFoundError: No module named 'code_critic'
__________ ERROR collecting agents/code_critic/tests/test_app_run.py ___________
ImportError while importing test module '/workspace/repo/agents/code_critic/tests/test_app_run.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/code_critic/tests/test_app_run.py:17: in <module>
    from code_critic.critique import Critique, Issue
E   ModuleNotFoundError: No module named 'code_critic'
__________ ERROR collecting agents/code_critic/tests/test_critique.py __________
ImportError while importing test module '/workspace/repo/agents/code_critic/tests/test_critique.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/code_critic/tests/test_critique.py:6: in <module>
    from code_critic.critique import (
E   ModuleNotFoundError: No module named 'code_critic'
____________ ERROR collecting agents/critic/tests/test_app_emit.py _____________
ImportError while importing test module '/workspace/repo/agents/critic/tests/test_app_emit.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/critic/tests/test_app_emit.py:11: in <module>
    from critic import app
E   ModuleNotFoundError: No module named 'critic'
_____________ ERROR collecting agents/critic/tests/test_app_run.py _____________
ImportError while importing test module '/workspace/repo/agents/critic/tests/test_app_run.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/critic/tests/test_app_run.py:16: in <module>
    from critic import app
E   ModuleNotFoundError: No module named 'critic'
____________ ERROR collecting agents/critic/tests/test_critique.py _____________
ImportError while importing test module '/workspace/repo/agents/critic/tests/test_critique.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/critic/tests/test_critique.py:8: in <module>
    from critic.critique import Critique, Issue, render_critique, severity_counts
E   ModuleNotFoundError: No module named 'critic'
______________ ERROR collecting agents/critic/tests/test_hooks.py ______________
ImportError while importing test module '/workspace/repo/agents/critic/tests/test_hooks.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/critic/tests/test_hooks.py:8: in <module>
    from critic.hooks import GET_ARTIFACT_CAP, build_hooks
E   ModuleNotFoundError: No module named 'critic'
___________ ERROR collecting agents/proposer/tests/test_app_async.py ___________
ImportError while importing test module '/workspace/repo/agents/proposer/tests/test_app_async.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/proposer/tests/test_app_async.py:18: in <module>
    from proposer import app
E   ModuleNotFoundError: No module named 'proposer'
____________ ERROR collecting agents/proposer/tests/test_app_run.py ____________
ImportError while importing test module '/workspace/repo/agents/proposer/tests/test_app_run.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/proposer/tests/test_app_run.py:18: in <module>
    from proposer import app
E   ModuleNotFoundError: No module named 'proposer'
___________ ERROR collecting agents/proposer/tests/test_proposal.py ____________
ImportError while importing test module '/workspace/repo/agents/proposer/tests/test_proposal.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/proposer/tests/test_proposal.py:8: in <module>
    from proposer.proposal import FileEdit, Proposal, ProposedIssue
E   ModuleNotFoundError: No module named 'proposer'
________ ERROR collecting agents/proposer/tests/test_research_agent.py _________
ImportError while importing test module '/workspace/repo/agents/proposer/tests/test_research_agent.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/proposer/tests/test_research_agent.py:12: in <module>
    from proposer.agent import compose_research_message
E   ModuleNotFoundError: No module named 'proposer'
_________ ERROR collecting agents/proposer/tests/test_research_app.py __________
ImportError while importing test module '/workspace/repo/agents/proposer/tests/test_research_app.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/proposer/tests/test_research_app.py:17: in <module>
    from proposer import app
E   ModuleNotFoundError: No module named 'proposer'
___________ ERROR collecting agents/retrospector/tests/test_agent.py ___________
ImportError while importing test module '/workspace/repo/agents/retrospector/tests/test_agent.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/retrospector/tests/test_agent.py:7: in <module>
    from retrospector.agent import compose_message
E   ModuleNotFoundError: No module named 'retrospector'
____________ ERROR collecting agents/retrospector/tests/test_app.py ____________
ImportError while importing test module '/workspace/repo/agents/retrospector/tests/test_app.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/retrospector/tests/test_app.py:11: in <module>
    from retrospector import app as retrospector_app
E   ModuleNotFoundError: No module named 'retrospector'
_________ ERROR collecting agents/retrospector/tests/test_decision.py __________
ImportError while importing test module '/workspace/repo/agents/retrospector/tests/test_decision.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/retrospector/tests/test_decision.py:8: in <module>
    from retrospector.decision import RetrospectiveDecision
E   ModuleNotFoundError: No module named 'retrospector'
___________ ERROR collecting agents/reviewer/tests/test_app_async.py ___________
ImportError while importing test module '/workspace/repo/agents/reviewer/tests/test_app_async.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/reviewer/tests/test_app_async.py:18: in <module>
    from reviewer import app
E   ModuleNotFoundError: No module named 'reviewer'
____________ ERROR collecting agents/reviewer/tests/test_app_run.py ____________
ImportError while importing test module '/workspace/repo/agents/reviewer/tests/test_app_run.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/reviewer/tests/test_app_run.py:20: in <module>
    from reviewer import app
E   ModuleNotFoundError: No module named 'reviewer'
_____________ ERROR collecting agents/reviewer/tests/test_hooks.py _____________
ImportError while importing test module '/workspace/repo/agents/reviewer/tests/test_hooks.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/reviewer/tests/test_hooks.py:8: in <module>
    from reviewer.hooks import GET_ARTIFACT_CAP, build_hooks
E   ModuleNotFoundError: No module named 'reviewer'
____________ ERROR collecting agents/reviewer/tests/test_review.py _____________
ImportError while importing test module '/workspace/repo/agents/reviewer/tests/test_review.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/reviewer/tests/test_review.py:8: in <module>
    from reviewer.review import (
E   ModuleNotFoundError: No module named 'reviewer'
____________ ERROR collecting agents/tester/tests/test_app_async.py ____________
ImportError while importing test module '/workspace/repo/agents/tester/tests/test_app_async.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/tester/tests/test_app_async.py:18: in <module>
    from tester import app
E   ModuleNotFoundError: No module named 'tester'
_____________ ERROR collecting agents/tester/tests/test_app_run.py _____________
ImportError while importing test module '/workspace/repo/agents/tester/tests/test_app_run.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/tester/tests/test_app_run.py:19: in <module>
    from tester import app
E   ModuleNotFoundError: No module named 'tester'
______________ ERROR collecting agents/tester/tests/test_hooks.py ______________
ImportError while importing test module '/workspace/repo/agents/tester/tests/test_hooks.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/tester/tests/test_hooks.py:8: in <module>
    from tester.hooks import GET_ARTIFACT_CAP, build_hooks
E   ModuleNotFoundError: No module named 'tester'
_____________ ERROR collecting agents/tester/tests/test_report.py ______________
ImportError while importing test module '/workspace/repo/agents/tester/tests/test_report.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/tester/tests/test_report.py:8: in <module>
    from tester.report import (
E   ModuleNotFoundError: No module named 'tester'
______________ ERROR collecting agents/triage/tests/test_agent.py ______________
ImportError while importing test module '/workspace/repo/agents/triage/tests/test_agent.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/triage/tests/test_agent.py:11: in <module>
    from triage import agent as triage_agent
E   ModuleNotFoundError: No module named 'triage'
____________ ERROR collecting agents/triage/tests/test_app_emit.py _____________
ImportError while importing test module '/workspace/repo/agents/triage/tests/test_app_emit.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
agents/triage/tests/test_app_emit.py:11: in <module>
    from triage import app
E   ModuleNotFoundError: No module named 'triage'
____ ERROR collecting lambdas/artifact_tool/tests/test_artifact_handler.py _____
ImportError while importing test module '/workspace/repo/lambdas/artifact_tool/tests/test_artifact_handler.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/artifact_tool/tests/test_artifact_handler.py:12: in <module>
    from artifact_tool.handler import handler, s3
E   ModuleNotFoundError: No module named 'artifact_tool'
______ ERROR collecting lambdas/entry_adapter/tests/test_entry_handler.py ______
ImportError while importing test module '/workspace/repo/lambdas/entry_adapter/tests/test_entry_handler.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/entry_adapter/tests/test_entry_handler.py:12: in <module>
    from aws_lambda_powertools.utilities.typing import LambdaContext
E   ModuleNotFoundError: No module named 'aws_lambda_powertools'
___ ERROR collecting lambdas/event_projector/tests/test_projector_handler.py ___
ImportError while importing test module '/workspace/repo/lambdas/event_projector/tests/test_projector_handler.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/event_projector/tests/test_projector_handler.py:14: in <module>
    from aws_lambda_powertools.utilities.typing import LambdaContext
E   ModuleNotFoundError: No module named 'aws_lambda_powertools'
_______ ERROR collecting lambdas/repo_helper/tests/test_repo_handler.py ________
ImportError while importing test module '/workspace/repo/lambdas/repo_helper/tests/test_repo_handler.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/repo_helper/tests/test_repo_handler.py:20: in <module>
    import repo_helper.handler as h
E   ModuleNotFoundError: No module named 'repo_helper'
____ ERROR collecting lambdas/retrospector_dispatcher/tests/test_handler.py ____
ImportError while importing test module '/workspace/repo/lambdas/retrospector_dispatcher/tests/test_handler.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/retrospector_dispatcher/tests/test_handler.py:11: in <module>
    from aws_lambda_powertools.utilities.typing import LambdaContext
E   ModuleNotFoundError: No module named 'aws_lambda_powertools'
_____ ERROR collecting lambdas/state_router/tests/test_circuit_breaker.py ______
ImportError while importing test module '/workspace/repo/lambdas/state_router/tests/test_circuit_breaker.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/state_router/tests/test_circuit_breaker.py:21: in <module>
    from state_router.actions import InvokeAgent, InvokeRepoHelper
E   ModuleNotFoundError: No module named 'state_router'
_________ ERROR collecting lambdas/state_router/tests/test_dispatch.py _________
ImportError while importing test module '/workspace/repo/lambdas/state_router/tests/test_dispatch.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/state_router/tests/test_dispatch.py:14: in <module>
    from state_router.actions import (
E   ModuleNotFoundError: No module named 'state_router'
____ ERROR collecting lambdas/state_router/tests/test_dispatch_contract.py _____
ImportError while importing test module '/workspace/repo/lambdas/state_router/tests/test_dispatch_contract.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/state_router/tests/test_dispatch_contract.py:21: in <module>
    from state_router.aws import dispatch_to_runtime
E   ModuleNotFoundError: No module named 'state_router'
_________ ERROR collecting lambdas/state_router/tests/test_executor.py _________
ImportError while importing test module '/workspace/repo/lambdas/state_router/tests/test_executor.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/state_router/tests/test_executor.py:16: in <module>
    from state_router.actions import InvokeAgent, InvokeRepoHelper, Noop
E   ModuleNotFoundError: No module named 'state_router'
______ ERROR collecting lambdas/state_router/tests/test_handler_batch.py _______
ImportError while importing test module '/workspace/repo/lambdas/state_router/tests/test_handler_batch.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/state_router/tests/test_handler_batch.py:19: in <module>
    from aws_lambda_powertools.utilities.typing import LambdaContext
E   ModuleNotFoundError: No module named 'aws_lambda_powertools'
__________ ERROR collecting lambdas/state_router/tests/test_model.py ___________
ImportError while importing test module '/workspace/repo/lambdas/state_router/tests/test_model.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
lambdas/state_router/tests/test_model.py:19: in <module>
    from state_router.model import parse_run
E   ModuleNotFoundError: No module named 'state_router'
_________ ERROR collecting services/dashboard/tests/test_artifacts.py __________
ImportError while importing test module '/workspace/repo/services/dashboard/tests/test_artifacts.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
services/dashboard/tests/test_artifacts.py:11: in <module>
    from dashboard.artifacts import (
E   ModuleNotFoundError: No module named 'dashboard'
___________ ERROR collecting services/dashboard/tests/test_events.py ___________
ImportError while importing test module '/workspace/repo/services/dashboard/tests/test_events.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
services/dashboard/tests/test_events.py:10: in <module>
    from fastapi.testclient import TestClient
E   ModuleNotFoundError: No module named 'fastapi'
_________ ERROR collecting services/dashboard/tests/test_run_detail.py _________
ImportError while importing test module '/workspace/repo/services/dashboard/tests/test_run_detail.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
services/dashboard/tests/test_run_detail.py:10: in <module>
    from fastapi.testclient import TestClient
E   ModuleNotFoundError: No module named 'fastapi'
________ ERROR collecting services/dashboard/tests/test_runs_delete.py _________
ImportError while importing test module '/workspace/repo/services/dashboard/tests/test_runs_delete.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
services/dashboard/tests/test_runs_delete.py:9: in <module>
    from fastapi.testclient import TestClient
E   ModuleNotFoundError: No module named 'fastapi'
__________ ERROR collecting services/dashboard/tests/test_webhooks.py __________
ImportError while importing test module '/workspace/repo/services/dashboard/tests/test_webhooks.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback:
services/dashboard/tests/test_webhooks.py:19: in <module>
    from fastapi import HTTPException
E   ModuleNotFoundError: No module named 'fastapi'
=============================== warnings summary ===============================
.venv/lib/python3.14/site-packages/bedrock_agentcore/runtime/context.py:17
  /workspace/repo/.venv/lib/python3.14/site-packages/bedrock_agentcore/runtime/context.py:17: PydanticDeprecatedSince20: Support for class-based `config` is deprecated, use ConfigDict instead. Deprecated in Pydantic V2.0 to be removed in V3.0. See Pydantic V2 Migration Guide at https://errors.pydantic.dev/2.13/migration/
    class RequestContext(BaseModel):

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
=========================== short test summary info ============================
ERROR packages/common/tests/test_browse_url.py
ERROR agents/architect/tests/test_agent.py
ERROR agents/architect/tests/test_app_async.py
ERROR agents/architect/tests/test_app_emit.py
ERROR agents/architect/tests/test_app_run.py
ERROR agents/architect/tests/test_hooks.py
ERROR agents/architect/tests/test_plan.py
ERROR agents/architect/tests/test_repo_grounding.py
ERROR agents/code_critic/tests/test_app_async.py
ERROR agents/code_critic/tests/test_app_run.py
ERROR agents/code_critic/tests/test_critique.py
ERROR agents/critic/tests/test_app_emit.py
ERROR agents/critic/tests/test_app_run.py
ERROR agents/critic/tests/test_critique.py
ERROR agents/critic/tests/test_hooks.py
ERROR agents/proposer/tests/test_app_async.py
ERROR agents/proposer/tests/test_app_run.py
ERROR agents/proposer/tests/test_proposal.py
ERROR agents/proposer/tests/test_research_agent.py
ERROR agents/proposer/tests/test_research_app.py
ERROR agents/retrospector/tests/test_agent.py
ERROR agents/retrospector/tests/test_app.py
ERROR agents/retrospector/tests/test_decision.py
ERROR agents/reviewer/tests/test_app_async.py
ERROR agents/reviewer/tests/test_app_run.py
ERROR agents/reviewer/tests/test_hooks.py
ERROR agents/reviewer/tests/test_review.py
ERROR agents/tester/tests/test_app_async.py
ERROR agents/tester/tests/test_app_run.py
ERROR agents/tester/tests/test_hooks.py
ERROR agents/tester/tests/test_report.py
ERROR agents/triage/tests/test_agent.py
ERROR agents/triage/tests/test_app_emit.py
ERROR lambdas/artifact_tool/tests/test_artifact_handler.py
ERROR lambdas/entry_adapter/tests/test_entry_handler.py
ERROR lambdas/event_projector/tests/test_projector_handler.py
ERROR lambdas/repo_helper/tests/test_repo_handler.py
ERROR lambdas/retrospector_dispatcher/tests/test_handler.py
ERROR lambdas/state_router/tests/test_circuit_breaker.py
ERROR lambdas/state_router/tests/test_dispatch.py
ERROR lambdas/state_router/tests/test_dispatch_contract.py
ERROR lambdas/state_router/tests/test_executor.py
ERROR lambdas/state_router/tests/test_handler_batch.py
ERROR lambdas/state_router/tests/test_model.py
ERROR services/dashboard/tests/test_artifacts.py
ERROR services/dashboard/tests/test_events.py
ERROR services/dashboard/tests/test_run_detail.py
ERROR services/dashboard/tests/test_runs_delete.py
ERROR services/dashboard/tests/test_webhooks.py
!!!!!!!!!!!!!!!!!!! Interrupted: 49 errors during collection !!!!!!!!!!!!!!!!!!!
make: *** [Makefile:52: test] Error 2

```
