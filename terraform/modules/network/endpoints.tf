################################################################################
# Gateway VPC endpoints for S3 and DynamoDB. Both are free and keep S3 +
# DynamoDB traffic on the AWS backbone instead of routing through the IGW.
#
# Interface endpoints were removed — the Fargate dashboard is the only
# VPC-resident workload, runs in public subnets with a public IP, and
# reaches other AWS APIs directly via the IGW. AgentCore Runtime uses
# network_mode = PUBLIC, and no Lambdas are VPC-attached, so nothing in
# the VPC needs private interface endpoints today.
################################################################################

module "vpc_endpoints" {
  source  = "terraform-aws-modules/vpc/aws//modules/vpc-endpoints"
  version = "~> 6.0"

  vpc_id = module.vpc.vpc_id

  endpoints = {
    s3 = {
      service         = "s3"
      service_type    = "Gateway"
      route_table_ids = module.vpc.private_route_table_ids
      tags            = { Name = "${local.name}-s3" }
    }
    dynamodb = {
      service         = "dynamodb"
      service_type    = "Gateway"
      route_table_ids = module.vpc.private_route_table_ids
      tags            = { Name = "${local.name}-dynamodb" }
    }
  }

  tags = merge(var.tags, {
    Component = "network"
  })
}
