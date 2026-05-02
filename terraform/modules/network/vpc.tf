################################################################################
# VPC + subnets + IGW + NAT + routing — delegated to terraform-aws-modules/vpc.
################################################################################

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = local.name
  cidr = var.vpc_cidr
  azs  = local.azs

  public_subnets  = var.public_subnets
  private_subnets = var.private_subnets

  enable_nat_gateway     = true
  single_nat_gateway     = !var.high_availability
  one_nat_gateway_per_az = var.high_availability

  enable_dns_hostnames    = true
  enable_dns_support      = true
  map_public_ip_on_launch = false

  public_subnet_tags  = { Tier = "public" }
  private_subnet_tags = { Tier = "private" }

  tags = merge(var.tags, {
    Component = "network"
  })
}
