################################################################################
# GitHub Actions OIDC provider. The OIDC audience condition (and subject claim
# scoping in the IAM trust policies) keeps assume-role narrow.
################################################################################

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]

  tags = merge(var.tags, {
    Name      = "${var.project}-github-actions-oidc"
    Component = "ci_cd"
  })
}
