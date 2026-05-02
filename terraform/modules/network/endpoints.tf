################################################################################
# VPC endpoints (PrivateLink). Keeps boto3 traffic from Lambdas, Fargate,
# and AgentCore Runtime (when in VPC mode) off the public internet.
################################################################################

module "vpc_endpoints" {
  source  = "terraform-aws-modules/vpc/aws//modules/vpc-endpoints"
  version = "~> 6.0"

  vpc_id             = module.vpc.vpc_id
  security_group_ids = [aws_security_group.vpc_endpoints.id]
  subnet_ids         = module.vpc.private_subnets

  endpoints = merge(
    {
      for s in local.interface_endpoints : s => {
        service             = s
        service_type        = "Interface"
        private_dns_enabled = true
        tags                = { Name = "${local.name}-${s}" }
      }
    },
    {
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
    },
  )

  tags = merge(var.tags, {
    Component = "network"
  })
}
