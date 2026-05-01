locals {
  name = "${var.project}-${var.env}"
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)

  interface_endpoints = toset([
    "bedrock-agentcore",
    "bedrock-agentcore-control",
    "bedrock-runtime",
    "secretsmanager",
    "kms",
    "logs",
    "monitoring",
    "events",
    "states",
    "sqs",
    "ecr.api",
    "ecr.dkr",
    "sts",
  ])
}
