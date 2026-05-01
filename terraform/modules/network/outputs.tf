output "vpc_id" {
  value = aws_vpc.this.id
}

output "public_subnet_ids" {
  value = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "agent_runtime_security_group_id" {
  value = aws_security_group.agent_runtime.id
}

output "lambda_security_group_id" {
  value = aws_security_group.lambda.id
}

output "vpc_cidr" {
  value = aws_vpc.this.cidr_block
}
