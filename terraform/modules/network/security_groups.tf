################################################################################
# Security groups consumed by AgentCore Runtime, Lambdas, and VPC endpoints.
################################################################################

resource "aws_security_group" "agent_runtime" {
  name        = "${local.name}-agent-runtime"
  description = "AgentCore Runtime ENIs (when VPC mode is enabled)."
  vpc_id      = aws_vpc.this.id

  egress {
    description = "Egress to AWS service endpoints."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "lambda" {
  name        = "${local.name}-lambda"
  description = "Lambda functions in private subnets."
  vpc_id      = aws_vpc.this.id

  egress {
    description = "Egress to AWS service endpoints."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "vpc_endpoints" {
  name        = "${local.name}-vpce"
  description = "VPC interface endpoints."
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "TLS from inside the VPC."
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}
