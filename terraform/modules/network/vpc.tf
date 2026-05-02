################################################################################
# VPC + subnets + IGW + routing — delegated to terraform-aws-modules/vpc.
#
# No NAT gateway: the only VPC-resident workload (the dashboard Fargate task)
# runs in a public subnet with a public IP for AWS API egress. Inbound is
# still gated by the ALB security group. Private subnets remain for future
# workloads but currently have no internet egress route.
################################################################################

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = local.name
  cidr = var.vpc_cidr
  azs  = local.azs

  public_subnets  = var.public_subnets
  private_subnets = var.private_subnets

  enable_nat_gateway = false

  enable_dns_hostnames    = true
  enable_dns_support      = true
  map_public_ip_on_launch = false

  public_subnet_tags  = { Tier = "public" }
  private_subnet_tags = { Tier = "private" }

  tags = merge(var.tags, {
    Component = "network"
  })
}
