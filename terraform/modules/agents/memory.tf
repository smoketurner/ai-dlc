################################################################################
# AgentCore Memory + 4 strategies. Memory namespaces:
#   * SEMANTIC          → /projects/{actorId}/facts                     (project-scoped)
#   * USER_PREFERENCE   → /users/{actorId}/preferences                  (user-scoped)
#   * SUMMARIZATION     → /sessions/{sessionId}/summary                 (session summaries)
#   * EPISODIC          → /episodes/{actorId}/{sessionId}               (per-(actor,session) episodes)
#
# Each AgentCore Memory can hold at most one of each built-in strategy type
# (limit: 6 strategies total). The EPISODIC strategy supports the HITL
# rejection-retry loop — when a reviewer rejects an ADR or PR, the agent
# can recall the actual sequence of prior attempts for the same project.
################################################################################

resource "aws_iam_role" "memory_execution" {
  name               = "${local.prefix}-memory-execution"
  assume_role_policy = data.aws_iam_policy_document.memory_assume.json
  description        = "Execution role assumed by AgentCore Memory for model inference."

  tags = merge(var.tags, {
    Name      = "${local.prefix}-memory-execution"
    Component = "agents"
  })
}

resource "aws_iam_role_policy_attachment" "memory_execution_inference" {
  role       = aws_iam_role.memory_execution.name
  policy_arn = data.aws_iam_policy.memory_inference.arn
}

resource "aws_bedrockagentcore_memory" "this" {
  name                      = local.memory_id
  description               = "Cross-session memory for ${var.project} ${var.env} agents."
  event_expiry_duration     = var.memory_event_expiry_days
  memory_execution_role_arn = aws_iam_role.memory_execution.arn

  tags = merge(var.tags, {
    Name      = local.memory_id
    Component = "agents"
  })
}

resource "aws_bedrockagentcore_memory_strategy" "semantic_project" {
  name        = "semantic_project"
  memory_id   = aws_bedrockagentcore_memory.this.id
  type        = "SEMANTIC"
  description = "Project-scoped semantic facts."
  namespaces  = ["/projects/{actorId}/facts"]
}

resource "aws_bedrockagentcore_memory_strategy" "user_preferences" {
  name        = "user_preferences"
  memory_id   = aws_bedrockagentcore_memory.this.id
  type        = "USER_PREFERENCE"
  description = "Per-user preferences carried across sessions."
  namespaces  = ["/users/{actorId}/preferences"]
}

resource "aws_bedrockagentcore_memory_strategy" "summarization_session" {
  name        = "summarization_session"
  memory_id   = aws_bedrockagentcore_memory.this.id
  type        = "SUMMARIZATION"
  description = "Per-session conversation summaries."
  namespaces  = ["/sessions/{sessionId}/summary"]
}

resource "aws_bedrockagentcore_memory_strategy" "episodic" {
  name        = "episodic"
  memory_id   = aws_bedrockagentcore_memory.this.id
  type        = "EPISODIC"
  description = "Per-(actor, session) episodes — supports HITL rejection-retry by recalling prior attempts."
  namespaces  = ["/episodes/{actorId}/{sessionId}"]
}
