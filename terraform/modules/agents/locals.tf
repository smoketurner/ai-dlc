locals {
  prefix     = "${var.project}-${var.env}"
  memory_id  = "${var.project}-${var.env}-memory"
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

  # Subset of agents that have a published image tag; runtime resources are
  # only provisioned for these. Skip an agent on first apply by leaving its
  # image_tag = "" until CI has pushed at least one image.
  runtime_agents = {
    for k, v in var.agents : k => v if v.image_tag != ""
  }
}
