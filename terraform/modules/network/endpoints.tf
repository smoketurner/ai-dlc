################################################################################
# Gateway VPC endpoints for S3 and DynamoDB. Both are free and avoid NAT
# data-processing charges for high-volume calls.
#
# Interface endpoints were removed in favour of NAT egress — the Fargate
# dashboard is the only VPC-resident workload, AgentCore Runtime uses
# network_mode = PUBLIC, and no Lambdas are VPC-attached. NAT covers the
# control-plane traffic the dashboard makes.
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
