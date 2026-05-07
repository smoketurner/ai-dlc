locals {
  aws_partition  = data.aws_partition.current.partition
  aws_account_id = data.aws_caller_identity.current.account_id
  aws_region     = data.aws_region.current.region

  prefix = "${var.project}-${var.env}"
  # AgentCore Memory names must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ — no hyphens.
  memory_id  = replace("${var.project}_${var.env}_memory", "-", "_")
  source_dir = "${path.module}/../../../lambdas"

  # Tool Lambda identifiers used as map keys for resources keyed by tool name.
  # Each tool Lambda corresponds to one entry in `var.agents[*].targets`.
  tools = toset(["artifact_tool", "repo_helper"])

  # Materialise the per-agent × per-target list so for_each can build one
  # gateway target per (agent, tool) pair.
  agent_targets = merge([
    for agent_name, cfg in var.agents : {
      for tool in cfg.targets :
      "${agent_name}.${tool}" => {
        agent = agent_name
        tool  = tool
      }
    }
  ]...)

  # Agents that mint GitHub installation tokens directly inside their
  # container (clone / commit / push). These need the App's private-key
  # secret in their env + IAM. Other agents delegate git ops to the
  # repo_helper Lambda and don't need direct access.
  #   * architect — clones the target repo for spec grounding.
  #   * implementer — clones, commits, pushes the task PR.
  github_app_direct_agents = toset(["architect", "implementer"])
}
