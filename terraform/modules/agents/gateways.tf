################################################################################
# Per-agent AgentCore Gateway. Each agent gets:
#   * its own gateway role (assume by bedrock-agentcore.amazonaws.com)
#   * its own gateway with Cognito JWT auth
#   * one gateway target per tool the agent is allowed to call
#
# AWS recommends a separate gateway per agent (clean blast radius, separate
# IAM/JWT scopes, easier to audit). Targets are routed to the shared tool
# Lambdas via the gateway role's lambda:InvokeFunction policy.
################################################################################

resource "aws_iam_role" "gateway" {
  for_each = var.agents

  name               = "${local.prefix}-${each.key}-gateway"
  assume_role_policy = data.aws_iam_policy_document.gateway_assume.json
  description        = "Role assumed by the AgentCore Gateway for the ${each.key} agent."

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}-gateway"
    Component = "agents"
  })
}

resource "aws_iam_role_policy" "gateway_invoke" {
  for_each = { for k, v in var.agents : k => v if length(v.targets) > 0 }

  name   = "invoke-tools"
  role   = aws_iam_role.gateway[each.key].id
  policy = data.aws_iam_policy_document.gateway_invoke[each.key].json
}

resource "aws_bedrockagentcore_gateway" "agent" {
  for_each = var.agents

  name            = "${local.prefix}-${each.key}"
  description     = each.value.description
  role_arn        = aws_iam_role.gateway[each.key].arn
  authorizer_type = "CUSTOM_JWT"
  protocol_type   = "MCP"

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = var.cognito_discovery_url
      allowed_audience = var.cognito_audience
    }
  }

  protocol_configuration {
    mcp {
      instructions       = "Gateway for the ${each.key} agent."
      search_type        = "SEMANTIC"
      supported_versions = ["2025-06-18"]
    }
  }

  tags = merge(var.tags, {
    Name      = "${local.prefix}-${each.key}"
    Component = "agents"
  })
}

# One gateway target per (agent, tool) pair.

resource "aws_bedrockagentcore_gateway_target" "artifact_tool" {
  for_each = {
    for k, v in local.agent_targets : k => v
    if v.tool == "artifact_tool"
  }

  name               = "artifact-tool"
  gateway_identifier = aws_bedrockagentcore_gateway.agent[each.value.agent].gateway_id
  description        = "S3 + MEMORY.md operations."

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.tool_lambda["artifact_tool"].lambda_function_arn

        tool_schema {
          inline_payload {
            name        = "artifact_tool"
            description = "Read, write, and list run artifacts and per-project MEMORY.md snapshots."

            input_schema {
              type = "object"

              property {
                name        = "op"
                type        = "string"
                description = "Operation: put_artifact | get_artifact | list_artifacts | read_memory_md | write_memory_md."
                required    = true
              }
              property {
                name        = "key"
                type        = "string"
                description = "S3 object key (used by put_artifact, get_artifact)."
              }
              property {
                name        = "content"
                type        = "string"
                description = "UTF-8 text content (used by put_artifact, write_memory_md)."
              }
              property {
                name        = "prefix"
                type        = "string"
                description = "Key prefix (used by list_artifacts)."
              }
              property {
                name        = "max_keys"
                type        = "integer"
                description = "Maximum keys to return (used by list_artifacts)."
              }
              property {
                name        = "project_slug"
                type        = "string"
                description = "Project slug (used by read_memory_md, write_memory_md)."
              }
              property {
                name        = "session_id"
                type        = "string"
                description = "Session id (used by write_memory_md)."
              }
            }
          }
        }
      }
    }
  }
}

resource "aws_bedrockagentcore_gateway_target" "repo_helper" {
  for_each = {
    for k, v in local.agent_targets : k => v
    if v.tool == "repo_helper"
  }

  name               = "repo-helper"
  gateway_identifier = aws_bedrockagentcore_gateway.agent[each.value.agent].gateway_id
  description        = "git / GitHub operations."

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = module.tool_lambda["repo_helper"].lambda_function_arn

        tool_schema {
          inline_payload {
            name        = "repo_helper"
            description = "Open PRs, comment on PRs, create branches, commit files, read PR state."

            input_schema {
              type = "object"

              property {
                name        = "op"
                type        = "string"
                description = "Operation: open_pr | comment_pr | create_branch | commit_files | get_pr."
                required    = true
              }
              property {
                name        = "repo"
                type        = "string"
                description = "GitHub repository in `owner/name` form."
              }
              property {
                name        = "pr_number"
                type        = "integer"
                description = "Pull request number (used by comment_pr, get_pr)."
              }
              property {
                name        = "branch"
                type        = "string"
                description = "Branch name (used by create_branch, commit_files)."
              }
              property {
                name        = "base"
                type        = "string"
                description = "Base branch / ref (used by open_pr, create_branch)."
              }
              property {
                name        = "head"
                type        = "string"
                description = "Head branch (used by open_pr)."
              }
              property {
                name        = "title"
                type        = "string"
                description = "PR title (used by open_pr)."
              }
              property {
                name        = "body"
                type        = "string"
                description = "PR or comment body."
              }
              property {
                name        = "message"
                type        = "string"
                description = "Commit message (used by commit_files)."
              }
              property {
                name        = "files"
                type        = "array"
                description = "List of {path, content} pairs to upsert (used by commit_files)."

                items {
                  type = "object"
                  property {
                    name     = "path"
                    type     = "string"
                    required = true
                  }
                  property {
                    name     = "content"
                    type     = "string"
                    required = true
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
