################################################################################
# AgentCore Code Interpreter — shared workspace-singleton resource for agents
# that need sandboxed code execution (currently Tester + Reviewer). Sessions
# are created on demand from the agent containers via the Bedrock AgentCore
# SDK; this resource just defines the workspace the sessions belong to.
#
# Network mode is PUBLIC so the sandbox can ``git clone`` the PR under test
# using a short-lived installation token minted by repo_helper.
# ``execution_role_arn`` is only required for SANDBOX mode.
################################################################################

resource "aws_bedrockagentcore_code_interpreter" "shared" {
  name        = replace("${local.prefix}_code_interpreter", "-", "_")
  description = "Shared code interpreter sandbox for ${var.project} ${var.env} agents (running PR tests/lint)."

  network_configuration {
    network_mode = "PUBLIC"
  }

  tags = merge(var.tags, {
    Name      = replace("${local.prefix}_code_interpreter", "-", "_")
    Component = "agents"
  })
}
