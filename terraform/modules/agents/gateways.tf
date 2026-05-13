################################################################################
# Per-agent AgentCore Gateway. Each agent gets:
#   * its own gateway role (assume by bedrock-agentcore.amazonaws.com)
#   * its own gateway with a Cognito CUSTOM_JWT authorizer. The agent
#     runtime exchanges its workload identity token for a Cognito-issued
#     M2M JWT via AgentCore Identity (M2M / client_credentials grant) and
#     forwards that JWT as the Bearer header on MCP tool calls. The
#     authorizer accepts JWTs whose ``client_id`` matches the M2M app
#     client provisioned in module.auth.
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

  # AgentCore Gateway names match ``^([0-9a-zA-Z][-]?){1,100}$`` —
  # alphanumerics with hyphen separators only. Agent keys can carry
  # underscores (e.g. ``code_critic``), so we replace ``_`` with ``-``
  # for the name. Keep ``each.key`` for IAM role / tag-Name fields
  # where underscores are valid.
  name            = "${local.prefix}-${replace(each.key, "_", "-")}"
  description     = each.value.description
  role_arn        = aws_iam_role.gateway[each.key].arn
  authorizer_type = "CUSTOM_JWT"
  protocol_type   = "MCP"

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = var.cognito_discovery_url
      allowed_clients = [var.cognito_gateway_m2m_client_id]
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
                description = "Operation: put_artifact | get_artifact | list_artifacts | read_memory_md | write_memory_md | read_stack_profile_md."
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
                description = "Operation: open_pr | comment_pr | create_branch | commit_files | get_pr | comment_issue | create_issue | list_issue_comments."
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
                name        = "issue_number"
                type        = "integer"
                description = "Issue number (used by comment_issue, list_issue_comments)."
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
                description = "PR or issue title (used by open_pr, create_issue)."
              }
              property {
                name        = "body"
                type        = "string"
                description = "PR / issue / comment body."
              }
              property {
                name        = "message"
                type        = "string"
                description = "Commit message (used by commit_files)."
              }
              property {
                name        = "labels"
                type        = "array"
                description = "GitHub labels to attach (used by create_issue)."

                items {
                  type = "string"
                }
              }
              property {
                name        = "parent_issue_url"
                type        = "string"
                description = "Backlink URL of the parent issue (used by create_issue to inject the `Spawned from` blockquote)."
              }
              property {
                name        = "requestor"
                type        = "string"
                description = "GitHub login of the human requestor (used by create_issue attribution)."
              }
              property {
                name        = "requestor_sub"
                type        = "string"
                description = "Cognito sub of the human requestor for on-behalf-of token minting (optional; falls back to the GitHub App installation token when absent)."
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
