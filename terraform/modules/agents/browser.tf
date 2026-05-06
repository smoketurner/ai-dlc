################################################################################
# AgentCore Browser — shared workspace-singleton resource for agents that
# need web research (currently the Proposer). Sessions are created on demand
# from the agent containers via the Bedrock AgentCore SDK; this resource just
# defines the workspace the sessions belong to.
#
# Network mode is PUBLIC: AWS-owned NAT egress, no VPC plumbing. The Proposer
# only browses public docs, so this matches the existing runtime stance. If
# we ever need to reach private resources from a browser session, switch to
# VPC mode and supply ``vpc_config`` (subnets must have outbound internet
# for browsing to work at all).
################################################################################

resource "aws_bedrockagentcore_browser" "shared" {
  name        = replace("${local.prefix}_browser", "-", "_")
  description = "Shared browser sandbox for ${var.project} ${var.env} agents (web research)."

  network_configuration {
    network_mode = "PUBLIC"
  }

  tags = merge(var.tags, {
    Name      = replace("${local.prefix}_browser", "-", "_")
    Component = "agents"
  })
}
