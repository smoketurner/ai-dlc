# Gateway migration plan

Migrate every agent's tool calls from direct `boto3` access (using the
runtime container's IAM role) onto the per-agent AgentCore Gateway via
MCP. Phased by agent so each migration ships independently, verifies
end-to-end on the dev account, and is reversible without affecting any
other agent.

## Why

The load-bearing reason: **identity propagation**. Today every agent's
`requestor_sub` field is plumbed through the dispatcher and agent
payloads but evaluates to `None` for issue-driven runs (which is
~100% of real traffic). Even when it's set, each agent has to
manually thread it into every `repo_helper` call. The pattern is
fragile — a single dropped field (the kind I just introduced in the
research-workflow dispatcher) silently regresses the audit trail to
bot attribution. Putting identity at the gateway layer makes
propagation a platform primitive: the runtime session is bound to a
user JWT at start, downstream tool calls inherit it. Manual threading
disappears.

Secondary reasons:

- **Containment of prompt-injection blast radius.** The runtime role
  today grants direct `s3:*`, `lambda:Invoke*`, `bedrock:*` etc. on
  load-bearing scopes. A successful prompt injection inside an agent
  container has the runtime role's full grant. Behind the gateway,
  the runtime can only invoke registered tool actions through the
  gateway; arbitrary AWS API access from inside the agent loop is no
  longer an option.

- **Architectural alignment with AgentCore.** Gateway-per-agent is
  the documented AWS pattern. New AgentCore capabilities (Cedar
  policies on tool calls, gateway-side observability, AgentCore
  Identity OAuth-on-behalf-of) plug in here. Going against the grain
  is what produced the WS 403 we just spent an hour debugging — those
  rough edges multiply when the ecosystem expects gateway-mediated
  access.

- **Tool versioning without redeploying agents.** Once tools are
  registered at the gateway, swapping a Lambda implementation behind
  a target doesn't require any agent change.

- **Per-tool observability.** Latency / error rate / cost metrics
  emitted at the gateway, sliced by tool, not by agent.

## Prerequisite: the capture problem

Identity propagation is moot if there's no identity to propagate.
Today the webhook handler at
`services/dashboard/src/dashboard/routes/webhooks.py:772-779` calls
`start_run(requestor=github_login, ...)` with no `requestor_sub` —
it captures a display name, not a Cognito subject. The DDB row is
written without the field; every downstream payload carries `None`.

Until that's fixed, the gateway-mediated path will pass `None` to
`repo_helper.token_for_call`, which falls back to the GitHub App
installation token. PRs and issue comments stay bot-attributed. The
gateway migration *prevents future regressions* of identity
propagation but doesn't *create* identity from nothing.

Three options to close the capture gap. They are independent of the
gateway migration — pick one as a parallel track.

| Option                                      | Work                                                                                                                                                                                       | Pros                                                                                          | Cons                                                                                                                                            |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| (a) GitHub login → Cognito sub mapping     | New DDB table or Cognito user-attribute. Dashboard login flow stores the link when a Cognito user authorizes their GitHub App install. Webhook handler queries by GitHub login at trigger. | Keeps Cognito as the principal identity layer (where dashboard auth + ALB authorizer live).   | Real engineering — table, link flow, webhook query, fallback when no link exists. Users have to log into the dashboard at least once.          |
| (b) AgentCore Identity GitHub federation    | Use AgentCore Identity's existing GitHub OAuth2 credential provider (already in `terraform/modules/agents/identity.tf`) as the user-identity layer. Cognito stays for dashboard chrome only. | The OAuth provider is already provisioned. Federation maps GitHub login → workload identity. | Larger architectural delta. Two identity providers in play (Cognito for dashboard, AgentCore Identity for agent flows) creates seams.            |
| (c) Drop `requestor_sub` everywhere         | Remove the field from `events.py`, `runtime.py`, `Run` model, every dispatcher payload, every agent input model, `repo_helper.handler`, `common/github_app.py:user_oauth_token_for_requestor_sub`. | Zero ambiguity — code matches deployed reality (always bot-attributed).                       | Forfeits the audit-trail story. Reversing later means re-plumbing.                                                                              |

This plan assumes one of (a) or (b) ships in parallel. Phase 7 below
(IAM trim + cleanup) blocks on the capture phase reaching the
"OBO actually fires" milestone.

## Tool-location policy

Three categories of "tool" the agents call. Each category has a
different home, and that placement is **stable across this
migration** — the gateway changes the *call mechanism* for category 1
only, not where the implementation lives. Use this table to decide
where any new tool belongs.

| # | Category                           | Home                                                                  | Examples                                                                                                                                                                                                                                                                                            | Why                                                                                                                                                                                                                                                                |
| - | ---------------------------------- | --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1 | Stateless remote API call          | Gateway-target Lambda in `lambdas/<tool>/`, registered in `terraform/modules/agents/gateways.tf` | `repo_helper`: open PR, comment on PR/issue, label, create branch, commit-via-API, get PR state, list issue comments, **create issue**. `artifact_tool`: read/write artifacts and per-project `MEMORY.md`.                                                                                          | Pure stateless API → Lambda fits. Gateway target adds MCP routing, Cedar policy hooks, and per-tool observability. Tool implementation is shared across all agents that have the target in their `var.agents[].targets` set.                                       |
| 2 | Local-checkout filesystem read     | Local Strands `@tool` function in the agent's package                 | `architect.repo_grounding.list_repo_paths`, `architect.repo_grounding.read_repo_file`. Future "diff a path" / "search the local checkout" helpers also belong here.                                                                                                                                  | Operates on the runtime container's filesystem at `/workspace/repo`. The gateway has no access to a runtime's local FS. Wrapping in a Lambda would mean mirroring the checkout to S3 or a shared volume — extra storage, extra latency, a stateful coordination problem. |
| 3 | Local git CLI on the working tree  | Implementer's `Bash` tool + git CLI; helpers in `agents/implementer/src/implementer/repo_ops.py` | `git checkout`, `git commit`, iterative `git push` during the implementer's loop. Branch creation as part of the working-tree workflow.                                                                                                                                                              | Implementer iteratively edits and commits during its loop. Routing every commit through a Lambda would batch-defer the commits and break the iterative model. After the loop ends, the post-agent code calls category-1 `repo_helper` to formalize PR open + final state. |

**Operational corollaries:**

- **Category 3 hands off to category 1 at the post-agent seam.**
  When implementer's `finish` MCP tool returns, the agent has produced
  commits on its local branch via category 3. The post-agent code in
  `agents/implementer/src/implementer/client.py` then issues
  category-1 `repo_helper` calls (open PR, set labels, etc.) to make
  those commits visible upstream. That seam stays after the gateway
  migration; only the call mechanism for the category-1 calls changes
  (direct Lambda invoke → MCP through gateway).
- **GitHub egress from the runtime container is split.** During
  implementer's loop, `git push` egresses directly to
  `api.github.com` from the container using the GitHub App
  installation token mounted in env — that's category 3, unchanged.
  After the loop, category-1 calls leave the container as MCP
  requests to the gateway, which invokes the `repo_helper` Lambda,
  which then talks to `api.github.com` from the Lambda's egress.
  Same App credential pool, two different network paths.

**Adding a new tool? Pick the category from this table:**

1. Does it call an external API and return a result with no
   dependence on container-local state? → **Category 1.** New Lambda
   in `lambdas/<tool>/`, register as a gateway target in
   `terraform/modules/agents/gateways.tf`, add to per-agent `targets`
   in `terraform/envs/dev/main.tf`. Update the gateway target's
   `input_schema` block to match the Lambda's Pydantic input.
2. Does it operate on a checkout, scratch directory, or any path
   under `/workspace/` in the container? → **Category 2.** New
   `@tool`-wrapped Python function in
   `agents/<agent>/src/<agent>/tools.py` (or in a shared `common/`
   module if multiple agents use it).
3. Does it manipulate the working tree iteratively in a way that
   depends on prior local state? → **Category 3.** Implementer-only
   territory. Express it as a sequence of `Bash` invocations or a
   helper in `implementer/repo_ops.py`.

If a proposed tool spans categories (e.g., "clone a repo *and* read
its contents"), split it: a category-1 op to fetch into local FS,
then category-2 ops to read. Avoid hybrids — they create the kind of
state-plus-RPC seam that's hard to debug when it fails.

## Current state — audit

Per-tool callsite mapping (full grep, not a sketch):

| Tool callsite                                                      | Today                                                                              | Category | Migrates? — How                                              |
| ------------------------------------------------------------------ | ---------------------------------------------------------------------------------- | -------- | ------------------------------------------------------------ |
| `common/memory_md.py:read_memory_md`                               | direct `boto3.client("s3").get_object` from runtime IAM                            | 1        | yes — `artifact_tool.read_memory_md` via gateway             |
| `architect/tools.py:write_spec_doc`                                | direct `boto3.client("s3").put_object` from runtime IAM                            | 1        | yes — `artifact_tool.put_artifact` via gateway               |
| `critic/tools.py:read_spec_doc`                                    | direct S3 from runtime IAM                                                         | 1        | yes — `artifact_tool.get_artifact` via gateway               |
| `reviewer/tools.py:read_spec_doc`                                  | direct S3                                                                          | 1        | yes — same as critic                                         |
| `tester/tools.py:read_spec_doc`                                    | direct S3                                                                          | 1        | yes — same as critic                                         |
| `tester/tools.py:read_memory_md` (local override)                  | direct S3                                                                          | 1        | yes — replace with `artifact_tool.read_memory_md`            |
| `proposer/tools.py:list_issue_comments`                            | direct `lambda_client().invoke(FunctionName=repo_helper, op="list_issue_comments")` | 1        | yes — `repo_helper.list_issue_comments` via gateway          |
| `proposer/app.py:invoke_repo_helper` (post-agent)                  | direct lambda invoke for `comment_issue`, `create_branch`, `commit_files`, `open_pr`, `create_issue` (proposed-issue spawn) | 1        | yes — `repo_helper.<op>` via MCP                              |
| `architect/repo_grounding.py:list_repo_paths,read_repo_file`       | local filesystem reads inside the runtime checkout at `/workspace/repo`            | 2        | no — local-only operation                                    |
| `architect/repo_grounding.py:clone_target_repo,sync_memory_md_from_clone` | local clone + local FS read; uses GitHub App token directly                | 3 (clone) + 2 (sync read) | no — clone is local-egress; sync read is local FS    |
| `implementer/repo_ops.py` + Bash                                   | iterative `git checkout/commit/push` via Bash; GitHub App token in container env   | 3        | no — iterative working-tree manipulation                     |
| `implementer/client.py` post-agent (after `finish`)                | direct lambda invoke for `repo_helper.open_pr`                                     | 1        | yes — `repo_helper.open_pr` via MCP                          |
| `common/agentcore_browser.py:browse_url`                           | `BrowserClient.start()` + Playwright connect_over_cdp                              | n/a      | no — AgentCore Browser is its own primitive (not a target)   |
| `common/sandbox.py:run_pr_in_sandbox`                              | direct AgentCore Code Interpreter                                                  | n/a      | no — same                                                    |
| `implementer/finish` MCP server (in-process)                       | `BedrockAgentCoreApp` MCP                                                          | n/a      | no — purely internal session-end signal                       |
| `proposer/tools.py:read_eval_aggregate,read_drift_report,read_rejection_summary,read_few_shot_summary` | direct S3 from runtime IAM                                                | 1 (with caveat) | recommended **stay direct** — proposer-internal eval substrate, not used by other agents; promoting to gateway adds maintenance for zero cross-agent benefit |

Gateway targets already provisioned in
`terraform/modules/agents/gateways.tf`: `artifact_tool`, `repo_helper`.
Per-agent target subset is set in `terraform/envs/dev/main.tf`'s
`var.agents[].targets`. Each agent's gateway has a Cognito-JWT
authorizer; the gateway role grants `lambda:InvokeFunction` on the
specific tool-Lambda ARNs.

Runtime role IAM grants today (per-agent, in
`terraform/modules/agents/runtime.tf`):
- `lambda:InvokeFunction` on tool Lambdas listed in
  `var.agents[].targets`. Used by `proposer/app.py:invoke_repo_helper`
  and (transitively) by Lambda environment-fed direct callers.
- `s3:GetObject/PutObject/ListBucket` on `artifacts_bucket` and
  `memory_md_bucket`. Used by every direct S3 tool.
- `bedrock-agentcore:*BrowserSession` etc. (kept).
- `bedrock-agentcore:*CodeInterpreter*` etc. (kept).
- Bedrock model invoke, EventBridge `events:PutEvents`, AgentCore
  Memory CRUD, AgentCore Identity workload-token mint (kept).

The Lambda ARNs themselves stay — only the call site moves. The
runtime-role grants for **lambda invoke** and **direct S3 on tool
buckets** are the trim targets.

## Target state

```
┌─────────────────┐    InvokeAgentRuntime    ┌────────────────────┐
│  state_router   │ ───────────────────────▶ │ Agent Runtime      │
└─────────────────┘                          │  (Strands or CASDK)│
                                             └─────┬──────────────┘
                                                   │ MCP / streamable-http
                                                   │ Bearer: workload JWT
                                                   ▼
                                             ┌────────────────────┐
                                             │ Per-agent Gateway  │
                                             │  (CUSTOM_JWT auth) │
                                             └─────┬──────────────┘
                                                   │ lambda:InvokeFunction
                                                   ▼
                                             ┌────────────────────┐
                                             │ Tool Lambdas       │
                                             │ artifact / repo    │
                                             └────────────────────┘
```

`browse_url`, `run_pr_in_sandbox`, `list_repo_paths`, `read_repo_file`
stay direct — they hit AgentCore Browser / Code Interpreter / local
filesystem, none of which are gateway targets.

## Foundation phase (Phase 0)

**Goal:** ship the auth helper + MCP client helper + universal
workload-identity wiring so subsequent per-agent phases just consume
them. Zero behavioral change after this phase — agents keep doing
direct calls.

**Files added:**

- `packages/common/src/common/gateway_auth.py` (new)
  - `gateway_jwt() -> str` mints a token via
    `bedrock_agentcore.identity.IdentityClient.get_workload_access_token`
    (or whichever method the SDK exposes for `WORKLOAD_FEDERATION`).
  - Process-cached client. Token cache keyed on `(workload_name,
    audience)` with TTL ≈ 80% of issued token's `exp`. Threading lock
    around refresh.
  - Reads `AIDLC_AGENT_WORKLOAD_NAME` from env. Raises a clear error
    if unset.
- `packages/common/src/common/gateway_tools.py` (new)
  - `gateway_mcp_client() -> MCPClient` constructs a Strands
    `MCPClient` whose `transport_callable` lazily builds a
    `streamablehttp_client(url=AIDLC_AGENT_GATEWAY_URL,
    headers={"Authorization": f"Bearer {gateway_jwt()}"})`.
  - `gateway_tools(client: MCPClient) -> list[MCPAgentTool]` returns
    the tool list (`client.list_tools_sync()`).
  - Lifecycle: caller is responsible for entering `client` as a
    context manager — `build_agent` will wrap with `with client:` and
    keep tools live for the agent's invocation.

**Files modified:**

- `terraform/modules/agents/runtime.tf:394-397`
  - Move `AIDLC_AGENT_WORKLOAD_NAME` out of the
    `contains(targets, "repo_helper") && github_app_secret_name != null`
    conditional. Make it unconditional for every agent that has a
    workload identity (which is every agent that consumes the
    gateway). The conditional today only fires for `repo_helper`
    consumers because that's the only existing user; once gateway
    auth is universal, every agent needs it.
- `packages/common/src/common/settings.py:40`
  - No change yet. The field stays. Phase 7 cleans it up.

**Tests:**

- `packages/common/tests/test_gateway_auth.py`
  - Mock `IdentityClient.get_workload_access_token` to return a
    token with a known `exp`. Assert `gateway_jwt()` caches until
    near expiry, then refreshes.
  - Assert error message when `AIDLC_AGENT_WORKLOAD_NAME` is unset.
  - Assert thread safety: hammer with 50 concurrent calls,
    `get_workload_access_token` invoked exactly once.
- `packages/common/tests/test_gateway_tools.py`
  - Mock `streamablehttp_client` and the MCP server. Assert
    `gateway_mcp_client()` constructs with the right URL + Bearer
    header. Assert `gateway_tools()` returns a non-empty list when
    the mock server advertises tools.

**Acceptance criteria:**

- `uv run pytest -q packages/common/tests/test_gateway_auth.py packages/common/tests/test_gateway_tools.py` green.
- `uv run ruff check packages/common/` clean.
- `uv run ty check packages/common/src/` clean.
- `terraform fmt -recursive terraform/` no diff.
- `terraform -chdir=terraform/envs/dev validate` success.
- Live check on dev: ssh into one agent's runtime, manually call
  `gateway_jwt()` — token is issued, decodes to a JWT with the
  expected audience claim. (Or skip live; rely on per-agent phases
  to expose problems.)

**Rollback:** revert the PR. The added files are unused by any
agent until Phase 1+. The terraform env-var move is harmless if
reverted (agents already running don't care that workload name is
present).

## Per-agent migration phases

Each agent gets its own PR. The PRs are mutually independent — the
unmigrated agents keep using direct calls, the migrated agent uses
MCP, and they coexist indefinitely. Order is risk-driven; you can
deviate.

Phases 1–6 below are templates. The shape is the same; only the
file paths and tool subsets differ.

### Phase 1 — architect (read-heavy, lowest risk)

**Why first:** architect's tool surface is read-only on the gateway
side (`read_memory_md` from `artifact_tool`). The write side
(`write_spec_doc`) is currently called from `architect/app.py` after
the agent loop returns the structured `SpecBundle` — that's
post-agent code, not in the agent's tool list. It can stay direct
during Phase 1 and migrate in Phase 5 (post-agent code migration).

**Files modified:**

- `agents/architect/src/architect/tools.py`
  - Drop `read_memory_md_tool = tool(read_memory_md)` (line ~69).
  - Keep `list_repo_paths_tool`, `read_repo_file_tool`,
    `write_spec_doc_tool`, `browse_url_tool` — those stay direct.
- `agents/architect/src/architect/agent.py`
  - Replace direct tool list with gateway tools + the local-only ones:
    ```python
    from common.gateway_tools import gateway_mcp_client, gateway_tools
    ...
    def build_agent(run_id: str) -> Agent:
        ...
        mcp_client = gateway_mcp_client()
        mcp_client.start()
        return Agent(
            ...,
            tools=[
                *gateway_tools(mcp_client),  # artifact_tool MCP shape
                list_repo_paths_tool,
                read_repo_file_tool,
                write_spec_doc_tool,         # stays direct for Phase 1
                browse_url_tool,
            ],
            ...,
        )
    ```
  - Note: `mcp_client.start()` here, not `with mcp_client:`. The
    client must outlive the `Agent` invocation. Add a `try/finally`
    or a small `closing` wrapper at call sites that own the agent
    object.
  - Update `generate_spec` to `try/finally` close `mcp_client`.

**Tests:**

- `agents/architect/tests/test_app_async.py`
  - Patch `gateway_mcp_client` to return a mock client whose
    `list_tools_sync()` returns a known fake tool list. Assert
    `build_agent` calls `client.start()` and includes the gateway
    tools in `Agent(tools=...)`.
  - Assert that on agent invocation completion, the client is closed
    (cleanup path).
- New test fixture: a `gateway_client_stub()` helper in
  `packages/common/tests/conftest.py` that other agents' tests can
  reuse.

**Live verification:**

- Trigger a real spec-driven run on the dev account against an issue
  with the architect routed.
- Watch CloudWatch dashboard for the architect's gateway —
  invocation count > 0, no 403/400/5xx.
- Spec PR opens with valid `requirements.md` / `design.md` /
  `tasks.md` content.
- Compare token cost before/after — expect within ±5% (gateway hop
  adds latency, not tokens).

**Acceptance criteria:**

- All architect unit tests green.
- Live spec-driven run produces a valid spec PR (one full cycle).
- Gateway CloudWatch metrics show `artifact_tool.read_memory_md`
  invocations during the run.
- Runtime IAM unchanged in this PR — `lambda:InvokeFunction` and
  S3 grants stay broad. Trim happens in Phase 7.

**Rollback:** revert the PR. Architect goes back to direct S3 reads
via `boto3`. No infrastructure changes to undo.

### Phase 2 — critic

**Files modified:**
- `agents/critic/src/critic/tools.py` — drop `read_memory_md_tool`,
  `read_spec_doc_tool`. Keep `browse_url_tool`.
- `agents/critic/src/critic/agent.py` — same MCP wiring as architect.
- `write_critique` (in `critic/tools.py`, called from `critic/app.py`
  post-agent) stays direct for now. Migrates in Phase 5.

**Tests:** mirror Phase 1.

**Live verification:** trigger a spec run, observe critic
runs, gateway shows `artifact_tool.get_artifact` calls during
critique.

**Acceptance:** same shape as Phase 1.

**Rollback:** revert.

### Phase 3 — reviewer

**Files modified:**
- `agents/reviewer/src/reviewer/tools.py` — drop `read_memory_md_tool`,
  `read_spec_doc_tool`. Keep `run_pr_in_sandbox_tool` (direct, hits
  Code Interpreter), `browse_url_tool`.
- `agents/reviewer/src/reviewer/agent.py` — MCP wiring.
- `write_review` (post-agent) stays direct, migrates in Phase 5.

**Tests + live + acceptance + rollback:** mirror Phase 1.

### Phase 4 — tester

**Files modified:**
- `agents/tester/src/tester/tools.py` — drop the local `read_memory_md`
  override and `read_spec_doc_tool`. Keep `run_pr_in_sandbox_tool`,
  `browse_url_tool`.
- `agents/tester/src/tester/agent.py` — MCP wiring.

**Notable:** tester has its own `read_memory_md` (returns empty
string on missing object) instead of using `common`'s version.
After migration, the gateway-routed `artifact_tool.read_memory_md`
returns whatever the Lambda returns — confirm the Lambda's response
shape on missing key matches what tester expects. The
`artifact_tool` Lambda today raises a `NoSuchKey` error rather than
returning empty; either change the Lambda's contract or have tester
swallow the error in its prompt.

**Tests + live + acceptance + rollback:** mirror Phase 1.

### Phase 5 — proposer (most complex)

**Why fifth:** proposer has three concerns to migrate together:

1. The agent's tool list (read-only S3 readers + browse_url).
2. The post-agent code in `proposer/app.py` that calls
   `lambda_client().invoke()` to comment on issues, create branches,
   commit files, open PRs.
3. The research-mode handler I just added — it threads `intent` and
   `issue_number` to the agent and posts comments via repo_helper.

**Sub-phase 5a — agent tool list:**

- `agents/proposer/src/proposer/tools.py` — drop `read_eval_aggregate_tool`,
  `read_drift_report_tool`, `read_rejection_summary_tool`,
  `read_few_shot_summary_tool`, `read_memory_md_tool`. These all read
  S3 directly today; once `artifact_tool` exposes the equivalent ops
  through MCP, the agent uses those.

  Note: `artifact_tool` does NOT today expose `read_eval_aggregate`
  etc. — those are proposer-specific S3 readers. Either:
  - Add new ops to `artifact_tool` Lambda + the gateway target's
    `input_schema`, OR
  - Keep these as direct readers (exception to the migration policy)
    because they're proposer-internal and not used by any other
    agent.

  Recommended: keep as direct (proposer-internal), migrate only the
  shared `read_memory_md` to gateway. Document the exception in the
  proposer's `tools.py` docstring.

- `agents/proposer/src/proposer/agent.py` — MCP wiring with the
  exception list.

**Sub-phase 5b — post-agent repo_helper migration:**

- `agents/proposer/src/proposer/app.py:invoke_repo_helper`
  (lines ~145-156) — currently calls `lambda_client().invoke(...)`.
  Replace with a tool call through the same `MCPClient` that the
  agent used:
  ```python
  def invoke_repo_helper(*, op: str, **fields: Any) -> dict[str, Any]:
      with gateway_mcp_client() as client:
          tool = next(t for t in client.list_tools_sync() if t.name == "repo_helper")
          return tool.invoke({"op": op, **fields})
  ```
  Or pass the already-open `mcp_client` from the run path through
  to `invoke_repo_helper` to avoid re-opening.

- `proposer/app.py:run_research` and `run_scheduled` — accept
  the `mcp_client` from the `run_proposer` driver, pass it down.

**Sub-phase 5c — research-path identity propagation:**

- `packages/common/src/common/runtime.py:ProposerInput` — add
  `requestor_sub: str | None = None`.
- `lambdas/state_router/src/state_router/dispatch_run.py:invoke_proposer_research`
  — include `"requestor_sub": run.requestor_sub` in payload.
- `agents/proposer/src/proposer/app.py:run_research` — pass
  `requestor_sub` to `post_research_comment` and `open_proposal_pr`.
- `agents/proposer/src/proposer/app.py:invoke_repo_helper` —
  forward `requestor_sub` (to the gateway, which forwards to the
  Lambda; the Lambda already knows what to do with it).

This sub-phase fixes the dropped-`requestor_sub` bug regardless of
whether the gateway migration is in flight. **Land it as a separate
PR if you want the bug fix without waiting for the migration.**

**Tests:** Phase 1 shape, plus:
- `agents/proposer/tests/test_research_app.py` — assert
  `requestor_sub` is forwarded through every call
  (`open_proposal_pr`, `post_research_comment`).
- `lambdas/state_router/tests/test_dispatch.py` — assert
  `invoke_proposer_research` payload includes `requestor_sub`.

**Live verification:** trigger an issue-driven research run.
Verify both the comment on the issue and any PR opened
include the right attribution. (If capture phase hasn't shipped,
attribution is still bot — verify the wiring at least carries the
None correctly without errors.)

**Acceptance + rollback:** mirror Phase 1, modulo the multi-sub-phase
complexity.

### Phase 6 — implementer (different paradigm)

Implementer is on Claude Agent SDK, not Strands. The integration
point is `ClaudeAgentOptions.mcp_servers` in
`agents/implementer/src/implementer/options.py`.

**Files modified:**

- `agents/implementer/src/implementer/options.py:62`
  - Extend `mcp_servers` with the gateway entry:
    ```python
    mcp_servers={
        FINISH_SERVER_NAME: finish_server,
        "gateway": {
            "type": "http",
            "url": os.environ["AIDLC_AGENT_GATEWAY_URL"],
            "headers": {"Authorization": f"Bearer {gateway_jwt()}"},
        },
    },
    ```
  - Open question: does Claude Agent SDK accept a `headers` callable
    for JWT refresh on long sessions, or is the bearer baked in at
    build time? If baked, refresh requires rebuilding `options` per
    session — already the case (each `build_options` is a fresh call
    per task).
- `options.py:53-63` `allowed_tools` — add MCP-prefixed entries:
  ```python
  "mcp__gateway__artifact_tool",
  "mcp__gateway__repo_helper",
  ```
- `agents/implementer/src/implementer/prompts.py` — add a paragraph
  teaching implementer that
  `mcp__gateway__repo_helper` is the canonical way to interact with
  the target repo (don't `Bash`-shell-out `git push` etc.); same
  for `mcp__gateway__artifact_tool` for spec / artifact reads.

**Tests:**

- `agents/implementer/tests/test_options.py` — assert the gateway
  MCP server is in `mcp_servers` and the prefixed tool names are
  in `allowed_tools`.
- Add a stub MCP server fixture for end-to-end implementer tests.

**Live verification:** trigger a real task PR. Confirm:
- Implementer reads spec via `mcp__gateway__artifact_tool`.
- Implementer doesn't shell out to `git push` for repo_helper-shaped
  ops (the prompt instructs against this).
- A real PR opens, attributed correctly.

**Acceptance + rollback:** mirror Phase 1.

## Phase 7 — runtime IAM trim

**Blocks on:** Phases 1–6 all merged AND verified end-to-end on dev
across multiple runs AND capture phase has reached "OBO actually
fires" milestone (otherwise we have nothing to lose if we trim).

**Files modified:**

- `terraform/modules/agents/runtime.tf:204-218` (per-agent runtime
  invoke policy)
  - Drop `lambda:InvokeFunction` resource grants for tool-Lambda
    ARNs. Keep only the workload-identity / Bedrock / EventBridge /
    AgentCore Memory / Browser / Code Interpreter grants.
- `terraform/modules/agents/runtime.tf` (per-agent S3 grants)
  - Drop `s3:GetObject/PutObject/ListBucket` on `artifacts_bucket`
    and `memory_md_bucket` for the runtime role. The agent no longer
    touches those buckets directly; the tool Lambdas (invoked via
    gateway) own the S3 access. Keep S3 only if the runtime
    legitimately needs it for run state / OTEL / cache (audit
    grep before removing).

**Verification:**
- `terraform plan` shows policy size shrinkage on each runtime role.
- After apply, run a spec-driven run. Confirm no `AccessDenied` in
  CloudTrail tied to the runtime role on tool-Lambda ARNs / artifacts
  bucket.
- Run a research run. Same check.

**Acceptance:** all runs complete successfully on the trimmed IAM.
CloudTrail clean. No regression in CloudWatch error rates.

**Rollback:** revert the terraform PR. Restored grants apply on next
apply. The agents themselves are unaffected — they're already on the
gateway path.

## Phase 8 — cleanup

**Files removed:**

- `packages/common/src/common/memory_md.py:read_memory_md` — if every
  consumer migrated, delete the function. Or convert to a thin
  wrapper that calls the gateway tool. (Audit grep first.)
- Per-agent `tools.py`: delete the now-unused direct-call wrappers.
  Each agent's `tools.py` keeps only the local-only tools
  (`list_repo_paths`, `read_repo_file`, `run_pr_in_sandbox`).
- `packages/common/src/common/settings.py:40` —
  `agentcore_gateway_url` field. Used only as documentation today;
  after migration, the env var is read directly by `gateway_tools`.

**Files modified:**

- `CLAUDE.md` — update the "Key directories" section to reflect that
  tool calls flow through the gateway. Update any prose that says
  "Strands' Agent and Claude Agent SDK's ClaudeSDKClient produce the
  same behaviour locally and on AgentCore Runtime" with a note about
  local dev needing JWT minting (or a local-mode bypass).
- `docs/MEMORY.md` and per-agent prompts — sweep for references to
  direct tool patterns; update to reference MCP-routed equivalents.

**Acceptance:** zero `boto3.client("s3")` or `lambda_client().invoke`
calls in agent code that ought to be gateway-routed (grep
verification). All tests green. Live runs still work.

**Rollback:** trivial — re-add the wrappers from git history. By
this stage, multiple agents depend on the gateway, so revert is
piecewise.

## Phase dependency graph

```
                        ┌─────────────────────┐
                        │ Phase 0: Foundation │
                        │   (auth + MCP help) │
                        └──────────┬──────────┘
                                   │
        ┌──────────┬──────────┬────┴────┬──────────┬──────────┐
        ▼          ▼          ▼         ▼          ▼          ▼
   ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌─────────┐ ┌────────────┐
   │ Phase  │ │ Phase  │ │ Phase  │ │ Phase  │ │ Phase 5 │ │  Phase 6   │
   │ 1 arch │ │ 2 crit │ │ 3 rev  │ │ 4 test │ │ proposer│ │implementer │
   └───┬────┘ └────┬───┘ └────┬───┘ └────┬───┘ └────┬────┘ └─────┬──────┘
       └──────────┴──────────┴──────────┴──────────┴────────────┘
                                   │
                            ┌──────┴───────┐
                            │   Phase 7    │   ← also blocks on capture
                            │   IAM trim   │      phase reaching OBO-fires
                            └──────┬───────┘      milestone
                                   │
                            ┌──────┴───────┐
                            │   Phase 8    │
                            │   Cleanup    │
                            └──────────────┘
```

Phases 1–6 can be reordered or shipped in parallel by separate
authors. Phase 7 is the only fan-in. Phase 8 is independent of
capture.

The capture phase (option a/b/c above) runs as a parallel track
with no blocking dependency until Phase 7's "OBO-fires" milestone.

## Risks

- **JWT expiry mid-call.** If a long agent invocation outlives its
  JWT, the next MCP tool call returns 401. Mitigate: cache TTL
  conservative (~80% of `exp`); refresh on `WWW-Authenticate: Bearer
  error="invalid_token"` response (handled in `gateway_auth.py`).
- **MCP tool schema drift.** The gateway target's `input_schema` in
  terraform must match the Lambda's Pydantic model. Add a contract
  test that round-trips a known payload through both.
- **Two-path coexistence breakage.** During Phases 1–6, some agents
  use MCP and others use direct. Both paths share the runtime IAM.
  As long as the runtime role keeps the broader grants until
  Phase 7, this works. Risk: someone trims IAM early. Mitigate: add
  a comment in `runtime.tf` blocking the trim until Phase 7.
- **Gateway throttling.** Gateway has its own quotas. If we hit them
  during a busy day, MCP calls fail where direct calls would have
  succeeded. Watch CloudWatch; raise quota proactively after Phase 1.
- **Local dev friction.** Running an agent on a laptop currently
  works with developer credentials. After migration, the agent needs
  a JWT — the laptop has no AgentCore workload identity. Either
  build a local-mode bypass that uses developer creds + direct S3
  (separate code path), or accept that local agent runs are no
  longer end-to-end. Decide before Phase 1.
- **OBO regression on non-migrated agents.** During Phase 1–6, only
  the migrated agents could *in principle* propagate user identity
  via the gateway. Non-migrated agents continue threading
  `requestor_sub` through their payloads. This is fine because none
  of them propagate it for issue-driven runs anyway (capture phase
  blocks that). After capture phase + all migrations, OBO works
  uniformly.

## Open questions

1. **JWT lifetime.** What does
   `bedrock-agentcore:GetWorkloadAccessToken` issue tokens for —
   minutes, hours, day? Drives the cache TTL.
2. **MCP connection pooling.** Does Strands `MCPClient` pool
   connections under streamable-http, or is each tool call a fresh
   HTTP request? Affects per-call latency budgets.
3. **Claude Agent SDK header refresh.** Does `mcp_servers[...]
   .headers` accept a callable for refresh, or is the JWT baked at
   build time?
4. **`artifact_tool` op coverage.** Today exposes
   `put_artifact / get_artifact / list_artifacts / read_memory_md /
   write_memory_md`. Sufficient for architect/critic/reviewer/tester
   migration. Check whether proposer's eval-readers
   (`read_eval_aggregate` etc.) need to be added to the gateway
   surface or stay direct (recommended: stay direct, proposer-only).
5. **Tool prefix.** Strands `MCPClient` accepts a `prefix` kwarg —
   defaults to none. Decide whether to use a `gateway__` prefix to
   visually distinguish gateway tools from local tools in the
   agent's tool list.
6. **Triage.** Today has `targets = []` (no gateway tools). If
   triage ever needs tools, fold it into Phase 1's pattern — but
   today there's nothing to migrate. No phase.
7. **Capture phase choice.** (a) GitHub login → Cognito mapping vs
   (b) AgentCore Identity GitHub federation vs (c) drop
   `requestor_sub`. Decide before Phase 7's IAM trim.

## Estimated scope

| Phase                                  | Files touched | LOC delta (rough) | Effort  |
| -------------------------------------- | ------------- | ----------------- | ------- |
| 0 Foundation                           | ~5            | +250              | 0.5 day |
| 1 Architect                            | ~3            | ±50               | 0.5 day |
| 2 Critic                               | ~3            | ±50               | 0.5 day |
| 3 Reviewer                             | ~3            | ±50               | 0.5 day |
| 4 Tester                               | ~3            | ±60 (incl. shape) | 0.5 day |
| 5 Proposer (3 sub-phases)              | ~6            | ±150              | 1 day   |
| 6 Implementer                          | ~3            | ±50               | 0.5 day |
| 7 IAM trim                             | ~1            | -100              | 0.25 day|
| 8 Cleanup                              | ~5            | -200              | 0.25 day|
| **Capture (parallel track)**           | ~5–10         | +200 (option a)   | 1–2 days|

Total: ~5 days focused, plus ~1–2 days for capture. Spread across
PRs that ship independently.
