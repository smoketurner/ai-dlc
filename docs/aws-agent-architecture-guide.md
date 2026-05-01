# Hosting, Communication, Memory & Task Completion for Agent Teams on AWS

*A focused architectural guide for startup builds. May 2026.*

## TL;DR — The Reference Architecture

For a startup, the highest-leverage default is:

- **Host on Amazon Bedrock AgentCore Runtime** (serverless, microVM-per-session, MCP + A2A protocols, scales to zero, no infra to manage). Build the agents themselves with **Strands Agents** (AWS's open-source framework, though LangGraph and Claude Agent SDK also deploy fine here).
- **Communicate via three layers**:
  1. **MCP** for agent ↔ tool (exposed through **AgentCore Gateway**, which converts your Lambdas, OpenAPI specs, and existing MCP servers into a single MCP endpoint with auth, observability, and semantic tool search).
  2. **A2A protocol** for agent ↔ agent across teams/vendors (AgentCore Runtime supports A2A natively on port 9000).
  3. **EventBridge + Step Functions + SQS** for the *deterministic plumbing* between agents — event routing, joins, retries, compensation. Don't make agents do this themselves.
- **Persist memory with AgentCore Memory**: short-term (raw conversation events, ≤365 days) and long-term (semantic / summary / user-preference / custom strategies) with hierarchical namespaces and KMS encryption.
- **Complete tasks** via the supervisor-worker pattern (Strands' Agents-as-Tools / Graph / Swarm primitives), with HITL checkpoints via AgentCore's persistent filesystem (suspend → human reviews → resume), and self-improvement via AgentCore's new **Recommendations + Batch Evaluations + A/B Tests** loop (preview, late April 2026).

**One mental model**: AgentCore is a *modular set of services*, not a monolith. Use Runtime + Gateway + Memory + Identity + Observability as your starting four; add Browser and Code Interpreter when needed. Each piece works independently with any framework (Strands, LangGraph, CrewAI, LlamaIndex) and any model (Bedrock-hosted Claude/Nova, OpenAI, Gemini).

---

## 1. Hosting agents on AWS

### The four-tier decision tree

| Tier | Service | Best for | Cold start | Pricing model |
|---|---|---|---|---|
| **1 — Default** | **AgentCore Runtime** | Long-running sessions (up to 8h), microVM isolation per session, streaming responses, MCP/A2A native, auto-scale to thousands of concurrent sessions | Sub-second (microVM boot) | Per-second active CPU + peak memory. **I/O wait is free** — you don't pay for time spent waiting on LLM responses or tool calls (claimed up to 3.3× cheaper CPU than pre-allocated alternatives) |
| **2 — Lightweight tools / fast tasks** | **AWS Lambda** (≤15 min) | Tools the agent calls; short tasks; event-triggered helper agents | ~100ms warm, sub-second cold | Per request + GB-second |
| **3 — Always-on or specialized runtime** | **AWS Fargate / ECS** | Stateful long-running agents that don't fit AgentCore's session model; custom containers needing GPUs | ~30s | Per vCPU/GB-hour |
| **4 — You need full control** | **EKS / EC2** | Multi-cluster orchestration, GPU pools, regulated workloads needing host-level isolation | Provisioned | Instance pricing |

For 90%+ of startups building agentic SDLC tooling: **Tier 1 (AgentCore Runtime) for the agent loops, Tier 2 (Lambda) for the tools the agents call.** The April 2026 AgentCore release added a *managed harness* (preview) that lets you deploy an agent in three API calls (config-defined, Strands-powered, exportable to code when you need control), plus the **AgentCore CLI** with CDK/Terraform support and a *persistent filesystem* that lets agents suspend mid-task and resume — which is exactly what you want for HITL gates.

### Why AgentCore Runtime as default

- **Session isolation**: Each user session gets a dedicated microVM (CPU, memory, filesystem). Prevents cross-session leakage — important when an agent is editing files, running shells, or holding tokens for multiple users.
- **Protocol-native**: HTTP+streaming for agents (port 8080, `/invocations`), MCP servers (port 8000, `/mcp`), or A2A servers (port 9000, `/`) — pick at deploy time. Stateless streamable HTTP, so scale-out is automatic.
- **VPC connectivity + PrivateLink** for accessing internal resources without going over the public internet.
- **Identity-aware**: integrates with AgentCore Identity → Cognito / Okta / Entra ID for inbound auth and OAuth-vault for outbound (GitHub, Slack, Jira, etc.).
- **Framework-agnostic**: works with Strands, LangGraph, CrewAI, LlamaIndex, custom code. Model-agnostic too.
- **Built-in tracing** (OpenTelemetry) with auto-instrumentation for agent reasoning steps, tool calls, and model interactions; ships traces to CloudWatch and any OTEL backend (Datadog, Langfuse, Arize, LangSmith, Grafana).

### What to use for the framework itself

If you're AWS-committed: **Strands Agents** (1.0 GA, July 2025). It's what AgentCore's harness is built on, used in production by Amazon Q Developer, AWS Glue, and Kiro. Model-driven (the LLM plans + chains tools rather than you encoding workflow), supports Bedrock + Anthropic + OpenAI + Gemini + Llama + Ollama + custom; native MCP and A2A; multi-agent primitives (Agents-as-Tools, Swarm, Graph, Workflow); session manager with S3/local backends; OpenTelemetry by default. The same code runs locally and on AgentCore without changes.

If you want maximum portability or non-AWS dual-deployment: **LangGraph** (most production mileage; durable execution; checkpointing). It deploys cleanly on AgentCore Runtime as a container.

If you're Claude-first: **Claude Agent SDK** (subagents, hooks, Skills) — also deployable on AgentCore.

> **Pragmatic startup advice**: Pick Strands if AWS-native. Pick LangGraph if you want the most portable + battle-tested option. Pick Claude Agent SDK if Claude is your model and you want first-class subagent isolation. All three deploy on AgentCore Runtime.

---

## 2. How agents communicate

There are **three communication concerns** that production teams keep distinct. Mixing them is a leading cause of brittle agent systems.

### 2a. Agent ↔ tool: MCP via AgentCore Gateway

**The pattern**: Don't let each agent manage its own tool credentials, OAuth flows, and version compatibility. Instead, expose all tools through a single MCP endpoint that the agent talks to.

**AgentCore Gateway** is purpose-built for this:
- Accepts **OpenAPI specs**, **Smithy models**, **Lambda functions**, or existing **MCP servers** as targets.
- Returns a single MCP endpoint URL that any MCP-compatible agent can use.
- Handles **inbound auth** (OAuth 2.0 / OAuth 2.1 with PKCE; integrates with Cognito, Okta, Auth0, Entra ID; supports both 2-legged client-credentials and 3-legged authorization-code flows).
- Handles **outbound auth** (per-actor, per-target token vault — credentials only released when the right user *and* right target match; OAuth refresh handled automatically; SigV4 for AWS-native targets).
- Provides **semantic tool search** — embeds tool name/description/parameter docs at sync time so agents can discover relevant tools instead of being given the entire catalog (helps avoid context bloat in agents with hundreds of tools).
- One-click integration for Salesforce, Slack, Jira, Asana, Zendesk.

**Why this matters**: The first time you have 4 agents × 12 tools × 3 environments, naively wiring credentials per-agent will cost you a week and then break in production. Gateway centralizes it.

**Best practice**: Run **multiple Gateway instances** — one per agent or per related-agent group — for clear ownership. AWS's own guidance is explicit on this: "many teams deploy multiple Gateway instances ... to maintain clear boundaries."

### 2b. Agent ↔ agent: A2A protocol (across teams/vendors) + direct invocation (within a team)

**Within your own team's agents**, use one of the Strands multi-agent primitives:
- **Agents-as-Tools**: Hierarchical delegation — a supervisor agent treats sub-agents as callable tools. Cleanest pattern for SDLC pipelines (Spec → Architect → Implementer → QA, each "called" by the supervisor).
- **Graph**: Typed handoffs and explicit edges — best when you want deterministic flow.
- **Swarm**: Peer-to-peer with handoffs — best when routing is genuinely dynamic.
- **Workflow**: Step-driven, deterministic for known sequences.

**Across teams or vendors** (e.g., your build agent calling a Salesforce or SAP agent), use **A2A protocol**. AgentCore Runtime is a transparent A2A proxy on port 9000:
- Containers run a stateless streamable HTTP server on `0.0.0.0:9000/`.
- Agent discovery via Agent Cards at `/.well-known/agent-card.json`.
- JSON-RPC payloads pass through unmodified.
- AgentCore adds enterprise SigV4/OAuth 2.0 auth and session isolation on top.

Strands 1.0 added native A2A support and **auto-generates the agent card** from your tool list, so you can publish an agent for cross-team consumption with no extra wiring.

### 2c. Agent ↔ workflow: EventBridge + Step Functions for the deterministic plumbing

This is the part most teams get wrong. **Don't make agents do orchestration that a workflow engine does better.** AWS's own prescriptive guidance is blunt: adding an agent to a deterministic workflow adds cost, latency, and a reasoning layer that provides no value.

The recommended pattern is:

- **EventBridge** = the event bus. Publish events like `SPEC.READY_FOR_ARCH`, `BUILD.READY_FOR_QA`, `QA.PASSED`. EventBridge rules route events to the right handler with filtering, schema registry validation, and replay.
- **Step Functions** = the stateful coordinator. Use it for **joins** (waiting for parallel agents to finish), **timeouts**, **retries**, **compensation** (saga pattern), and human-approval steps. Standard workflows handle long-running orchestration (up to 1 year); Express workflows for high-volume short-running.
- **SQS** = the buffered queue between asynchronous steps.
- **DynamoDB** = correlation IDs, idempotency keys, and lightweight state between agents.
- **Lambda** = adapters that translate events → AgentCore Runtime invocations.
- **S3** = artifacts (specs, ADRs, generated code, test reports) and prompt versions.

This is the **AI-DLC happy path on AWS**: a finite state machine in Step Functions where each transition triggers an agent (via Lambda → AgentCore InvokeAgentRuntime), agent output is persisted to S3 + DynamoDB, EventBridge announces the new state, and humans gate critical transitions.

### Communication checklist

- [ ] All tool access funnels through AgentCore Gateway (or a managed MCP gateway equivalent), never direct credentials in agent code.
- [ ] Inter-agent messaging within your team uses Strands primitives (Agents-as-Tools / Graph) for tight coupling, A2A only for cross-team/vendor.
- [ ] Cross-stage SDLC handoffs go through EventBridge (not direct agent-to-agent calls), so you get replay, dead-letter queues, and the ability to swap agents without rewiring.
- [ ] Long-running joins and HITL approvals live in Step Functions, not in agent prompts.
- [ ] Every event is versioned and validated against a schema in the EventBridge schema registry.

---

## 3. How agents persist memory

### AgentCore Memory: short-term + long-term, fully managed

Two memory types working in tandem:

**Short-term memory** — raw, immutable, chronological conversation events written via `CreateEvent`. Stored synchronously, preserves narrative order, organized by **actor** (user/agent identity) and **session**. Configurable retention up to 365 days. Events can be conversational (`USER`/`ASSISTANT`/`TOOL` messages) or **blob** (binary checkpoints / agent state).

**Long-term memory** — distilled insights extracted asynchronously from short-term events by **memory strategies**:

| Strategy | What it extracts | When to use |
|---|---|---|
| **Semantic** | Facts and structured knowledge as JSON records | Default for most agents — user prefs, domain entities, learned facts |
| **Summarization** | Session-level summaries | Long conversations where you need gist, not detail |
| **User Preferences** | Stable preference signals | Personalization-heavy agents |
| **Custom (built-in with overrides)** | Same pipeline, your prompts and your Bedrock model | When the built-in extraction prompts don't fit your domain |
| **Custom (full)** | Your own pipeline end-to-end | Maximum control, custom record schemas, custom models |

You can mix multiple strategies on a single memory resource.

### Retrieval API surface

- `list_events(...)` — recent raw conversation context (short-term).
- `search_long_term_memories(query, namespace_path, top_k)` — semantic search over distilled records.
- `get_memory_record(...)` / `list_memory_records(...)` — direct retrieval.
- `RetrieveMemoryRecords` — the workhorse for context hydration.

### Hierarchical namespaces

Memory records live under namespaces like `/strategies/{strategyId}/actors/{actorId}/` so you can scope:
- per-user (`/users/{userId}/facts`)
- per-team (`/teams/{teamId}/decisions`)
- per-project (`/projects/{slug}/architecture`)

This is also your **access control boundary** — namespaces map to fine-grained IAM/Identity policies.

### Best practices for memory

1. **Use targeted retrieval**, not a kitchen-sink dump. Recent context → `list_events`. Session gist → summarization records. Learned facts → semantic search. Mixing them bloats the context window.
2. **Long-term extraction is async** — there's a ~few-second-to-minute delay between writing an event and it appearing in long-term records. Don't depend on read-after-write for time-sensitive flows; combine with short-term `list_events` for the most recent turns.
3. **Encrypt with customer-managed KMS keys** if you handle anything sensitive. Default is AWS-managed keys, which is fine for non-regulated workloads.
4. **Set explicit event expiry** per memory resource (up to 365 days). For SDLC agents, 30–90 days is usually enough; older context is rarely useful and creates compliance liability.
5. **Treat memory as a security boundary**. Use actor IDs and namespaces deliberately. Junior-analyst agents should not be able to read executive-tier facts even if they share the same memory resource.
6. **For coding agents specifically**, consider a hybrid: AgentCore Memory for cross-session insights ("user prefers TypeScript over JavaScript", "team uses pnpm not npm"), plus per-project `MEMORY.md` files in the persistent filesystem for repository-specific context. The persistent filesystem (preview, April 2026) makes this clean — agents suspend, the file persists, agents resume with full context.
7. **Don't store raw secrets in memory**. Tokens belong in AgentCore Identity's vault, not in conversation history.

---

## 4. How agents complete tasks

Three patterns cover essentially all SDLC use cases. Pick by *dependency structure*, not by what's trendy.

### Pattern A: Supervisor-Worker (orchestrator agent + specialists)

**When**: Tasks decompose into parallel or sequential strands with a clear coordinator. This is the AI-DLC default.

**How**: A supervisor agent receives the high-level intent, decomposes it, and dispatches to specialist agents (PM, architect, implementer, QA, security, SRE, doc) — each with its own system prompt, tool subset, and memory namespace. Use Strands' Agents-as-Tools primitive: each specialist is exposed as a tool the supervisor can call.

**State**: Step Functions tracks the overall state machine; each transition invokes the supervisor; the supervisor reads the current ADR/spec/code from S3 and decides which specialist to call next.

### Pattern B: Event-Driven Choreography

**When**: Loosely-coupled agents that need to react to events without a central coordinator (e.g., a code-push event triggers test-gen + security-scan + doc-update agents in parallel).

**How**: EventBridge as the bus, agents as subscribers, DynamoDB for correlation. Each agent does its piece and emits a follow-up event. Step Functions joins them when a downstream step needs *all* of them to have completed.

### Pattern C: Saga (long-running with compensation)

**When**: Multi-step workflows where partial failures need explicit rollback (e.g., create branch → generate code → run tests → deploy preview → request review; if any step fails, undo the prior steps).

**How**: Step Functions with `Catch` blocks invoking compensation Lambdas. Each step is idempotent (DynamoDB idempotency keys). This is essentially the proven AWS saga pattern applied to agents.

### Human-in-the-loop checkpoints

The April 2026 AgentCore release made this much cleaner. The persistent filesystem lets agents:
1. Reach a checkpoint (e.g., "ADR drafted, awaiting architect approval").
2. Suspend — write state to filesystem, exit.
3. Wait for a human action (Slack approval, PR comment, EventBridge event).
4. Resume from exactly where they paused, with full context and tool history intact.

For SDLC pipelines, **mandatory** HITL gates are:
- After requirements (PM agent → human PM signs off on spec).
- After architecture (architect agent → human reviews ADR).
- Before merge (QA + security agents → human reviews PR).
- Before production deploy (always human-approved).
- On any database write or `rm`-like action (always human-approved unless the agent is in a fully sandboxed environment).

### The closed self-improvement loop (preview, April 2026)

AgentCore now ships a complete observe-evaluate-improve loop:

1. **Observe**: AgentCore Observability captures every trace (token usage, latency, tool calls, decisions, errors) and ships them to CloudWatch + any OTEL backend.
2. **Evaluate**: AgentCore Evaluations scores agents against your test sets on correctness, helpfulness, safety, goal-success rate.
3. **Improve**: AgentCore Recommendations analyzes production traces and eval outputs to propose **optimized system prompts and tool descriptions** tailored to your workload.
4. **Validate**: Batch Evaluations test the recommendations against your test set; A/B Tests run them against live traffic with statistical significance reporting.
5. **Approve**: Every recommendation requires explicit human approval before it ships.

This is **eval-driven self-improvement** — exactly the closed loop production teams have been building manually with DSPy + GEPA + Langfuse, now offered as a managed AWS-native service. For a startup, this is the path of least resistance to "self-improving agents" that doesn't involve research-grade self-modifying code.

---

## 5. Production best practices summary

Drawn from AWS prescriptive guidance (the "9 best practices" blog, Feb 2026), the AI-DLC methodology, and recurring lessons from production deployments.

### Architecture

1. **Start narrow.** Pick one workflow with clear value (RFP triage, sales-quote assembly, SDLC slug-based delivery). Define ≤3 events. Stand up two agents (e.g., Router + Writer). Add a knowledge base of 20–50 clean documents. *Then* expand.
2. **Composability over monoliths.** Use AgentCore services *independently* — Memory without Runtime, Gateway without Identity, etc. Don't lock yourself in to using all of them.
3. **Multiple Gateway instances**, not one mega-gateway. Per-agent or per-related-agent-group.
4. **Strands or LangGraph for the agent loops, Step Functions for the deterministic plumbing.** Don't make agents do orchestration that a workflow engine does better.
5. **Persistent filesystem for HITL** — easier than rolling your own suspend/resume.

### Security & identity

6. **Identity-first.** Every agent gets a distinct identity in AgentCore Identity. Inbound auth via your IdP (Cognito/Okta/Entra). Outbound auth via the OAuth vault — never embed credentials in agent code or memory.
7. **Defense in depth on Gateway.** Auth at Gateway, IAM at the AWS service, Cedar policies via AgentCore Policy. If a junior-analyst agent tries to access executive-comp data, deny it at Gateway before it reaches the database.
8. **Threat-model with MAESTRO.** AWS published guidance for agentic threat modeling — prompt injection, tool poisoning, jailbreaks, exfiltration via tool combinations. Run this exercise before you ship.
9. **Mandatory sandboxing for code execution.** Use AgentCore Code Interpreter (managed sandbox) or AgentCore Browser for web actions. Never let an agent run unsandboxed shell on infra you care about.
10. **Container immutability + IaC.** Deploy via CDK or Terraform; pin container digests; pin MCP server versions in Gateway.

### Memory & data

11. **Use namespaces as a security boundary**, not just an organizational one.
12. **Customer-managed KMS keys** for any sensitive memory.
13. **Time-bound retention** — set event expiry to the minimum useful window.
14. **Don't store secrets in memory.** Tokens go in Identity vault.

### Observability & ops

15. **Trace everything from day one.** AgentCore's auto-instrumentation + OpenTelemetry + a backend (CloudWatch, Langfuse, Datadog). You will need this within the first month, guaranteed.
16. **Track aggregate + individual.** Dashboards for token usage, latency, session duration, error rates, tool-call patterns *and* the ability to drill into a single failed session. Both matter.
17. **Build the eval set on day one.** 50–200 representative tasks. Score with AgentCore Evaluations or LLM-as-Judge. This is your single most important asset for shipping safely.
18. **Game-day failures.** Break a tool, bump an event-schema version, hit rate limits, inject a malicious input. Fix what fails before customers find it.
19. **Cost guardrails per agent.** Set spend caps. Token usage explains most of the variance in agent cost; route Haiku/Nova-Lite for cheap work, Claude Sonnet for default, Claude Opus / Nova Premier only for orchestration and architectural reasoning.

### Process

20. **Working backwards from a use case.** Don't start with "what can an agent do?" Start with "what's the specific job-to-be-done and what's the success metric?"
21. **Mandatory HITL at SDLC stage transitions.** Spec→Arch, pre-merge, pre-deploy, production-write. Non-negotiable.
22. **Every event versioned.** Schema-first. EventBridge schema registry catches breakage early.
23. **Audit trail by construction.** Every agent decision → trace; every transition → event; every artifact → S3 with the project slug. AI-DLC compliance falls out of this for free.
24. **Game-day cadence.** Quarterly chaos test, monthly schema audit, weekly trace review.

---

## A worked reference architecture (SDLC startup)

Putting it all together for an end-to-end SDLC pipeline (Spec → Arch → Build → Test → Security → Deploy → Doc):

```
┌─────────────────────────────────────────────────────────────────┐
│                         User / Slack / API                       │
└─────────────────────────┬───────────────────────────────────────┘
                          │ (Cognito / Okta auth)
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  API Gateway → Lambda (entry adapter) → EventBridge bus          │
└─────────────────────────┬───────────────────────────────────────┘
                          │ event: REQUEST.RECEIVED
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  Step Functions State Machine (the SDLC orchestrator)            │
│  ─────────────────────────────────────────────────────────────  │
│   spec → arch → build → test → security → deploy → doc           │
│   each transition: invoke AgentCore Runtime (specialist agent)   │
│   with HITL approval steps between arch, test, deploy            │
└──┬──────────┬──────────┬──────────┬──────────┬──────────┬──────┘
   │          │          │          │          │          │
   ▼          ▼          ▼          ▼          ▼          ▼
 PM-spec   Architect  Implementer  QA-test  Security    SRE-deploy
 agent     agent      agent        agent    agent       agent
 (AgentCore Runtime — microVM per session, Strands/LangGraph)
                          │
                          │ all agents share:
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  AgentCore Gateway (single MCP endpoint)                         │
│  ↑ targets: GitHub MCP, Linear/Jira MCP, Postgres MCP, Sentry,   │
│            internal Lambdas (test-runner, deploy-runner, etc.)    │
│  ↑ inbound: OAuth 2.1 + PKCE                                     │
│  ↑ outbound: per-actor token vault via AgentCore Identity        │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  AgentCore Memory (per-project + per-user namespaces)            │
│  - Short-term: session events                                    │
│  - Long-term: semantic facts ("team uses pnpm"), summaries       │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  S3 (artifacts: specs, ADRs, code, reports, prompts)             │
│  DynamoDB (correlation IDs, idempotency, state)                  │
│  Persistent filesystem (HITL suspend/resume per agent session)   │
└─────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│  AgentCore Observability → CloudWatch + Langfuse/Datadog/Arize   │
│  AgentCore Evaluations → batch tests + A/B → Recommendations     │
│  All approved by humans before ship.                             │
└─────────────────────────────────────────────────────────────────┘
```

### Day-1 build order

1. **Set up AgentCore Identity** with Cognito (or your IdP). Define agent identities for each specialist.
2. **Stand up one AgentCore Gateway** with 3 targets: GitHub (MCP), Linear or Jira (MCP), one internal Lambda. Wire OAuth.
3. **Deploy one AgentCore Runtime** — start with the implementer agent. Use Strands. Confirm tracing flows to CloudWatch.
4. **Create AgentCore Memory** with a semantic strategy. Wire it to the implementer agent.
5. **Define one Step Functions workflow**: receive request → invoke implementer agent → wait for human approval → done. Just two states. Make it work end-to-end.
6. **Build the eval set** (10 tasks is enough to start). Run AgentCore Evaluations.
7. **Add the next agent** (architect, then QA, etc.). Each new agent: new identity, new memory namespace, new Step Functions state.
8. **At ~5 agents**, introduce EventBridge for cross-stage events and a second Gateway instance for the QA/security agents (separation of concerns).

That's a 4–6 week buildout for one SDLC pipeline if a single engineer is focused on it.

---

## Caveats

- **AgentCore is in preview** — pricing, regions, and APIs continue to change. Confirm against the AWS docs at deploy time. Several of the features cited here (managed harness, persistent filesystem, Recommendations + Batch Evals + A/B, AgentCore CLI, AgentCore Skills for coding assistants) shipped in late April / early May 2026 and are preview-tier.
- **Vendor numbers are vendor numbers.** AWS's "3.3× cheaper CPU" and "10–15× productivity" claims are upper bounds, not norms.
- **Strands is young** (1.0 in July 2025; ~2,000 GitHub stars at 1.0; 150K PyPI downloads). It's used in production at Amazon teams, but its ecosystem is smaller than LangGraph's. If you want the largest community + most production mileage today, LangGraph remains a defensible choice and runs on AgentCore identically.
- **AgentCore Memory's long-term extraction is asynchronous** — if your agent depends on read-after-write semantics, you need to combine short-term `list_events` with long-term retrieval. Don't assume newly-written facts are immediately searchable.
- **Lock-in considerations**: AgentCore Identity, Gateway, and Memory are AWS-only. The agents themselves (Strands, LangGraph) and the protocols (MCP, A2A) are portable. If avoiding AWS lock-in matters, build your agents in Strands or LangGraph and treat AgentCore services as substitutable infrastructure (you can run an MCP gateway, a memory store, and an identity service on any cloud — it'll just take more glue code).
- **Cost is unpredictable until you measure.** Token usage dominates; multi-agent setups can use 10–15× the tokens of a single agent. Set per-session budget caps and per-agent monthly caps before you turn on any kind of agent autonomy.
