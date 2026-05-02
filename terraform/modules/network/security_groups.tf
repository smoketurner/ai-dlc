################################################################################
# Security groups consumed by AgentCore Runtime, Lambdas, and VPC endpoints.
################################################################################

resource "aws_security_group" "agent_runtime" {
  name        = "${local.name}-agent-runtime"
  description = "AgentCore Runtime ENIs (when VPC mode is enabled)."
  vpc_id      = module.vpc.vpc_id

  egress {
    description = "Egress to AWS service endpoints."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name      = "${local.name}-agent-runtime"
    Component = "network"
  })
}

resource "aws_security_group" "lambda" {
  name        = "${local.name}-lambda"
  description = "Lambda functions in private subnets."
  vpc_id      = module.vpc.vpc_id

  egress {
    description = "Egress to AWS service endpoints."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, {
    Name      = "${local.name}-lambda"
    Component = "network"
  })
}

