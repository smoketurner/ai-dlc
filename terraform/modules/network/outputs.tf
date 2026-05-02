output "vpc_id" {
  value = module.vpc.vpc_id
}

output "public_subnet_ids" {
  value = module.vpc.public_subnets
}

output "private_subnet_ids" {
  value = module.vpc.private_subnets
}

output "agent_runtime_security_group_id" {
  value = aws_security_group.agent_runtime.id
}

output "lambda_security_group_id" {
  value = aws_security_group.lambda.id
}

output "vpc_cidr" {
  value = module.vpc.vpc_cidr_block
}
