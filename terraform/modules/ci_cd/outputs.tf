output "terraform_role_arn" {
  value = aws_iam_role.terraform.arn
}

output "image_publisher_role_arn" {
  value = aws_iam_role.image_publisher.arn
}

output "oidc_provider_arn" {
  value = aws_iam_openid_connect_provider.github.arn
}
