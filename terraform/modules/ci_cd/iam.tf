################################################################################
# Roles assumed by GitHub Actions:
#   * terraform        — plan on PR, apply on push to main.
#   * image_publisher  — push images to ECR after build.
################################################################################

resource "aws_iam_role" "terraform" {
  name               = "${var.project}-github-actions-terraform"
  assume_role_policy = data.aws_iam_policy_document.terraform_assume.json
  description        = "Assumed by GitHub Actions for terraform plan/apply."

  tags = merge(var.tags, {
    Name      = "${var.project}-github-actions-terraform"
    Component = "ci_cd"
  })
}

resource "aws_iam_role_policy" "terraform_inline" {
  name   = "terraform-inline"
  role   = aws_iam_role.terraform.id
  policy = data.aws_iam_policy_document.terraform_inline.json
}

# Administrator access for terraform apply. The trust policy already restricts
# WHO can assume this role (only PR + main on this repo via OIDC), so the
# blast radius is bounded by the GitHub side. PowerUserAccess was tried first
# but excludes IAM, which terraform needs for module.agents/dashboard role
# creation. Narrow to a custom inline policy if/when the org's CI/CD posture
# requires least-privilege at this layer.
resource "aws_iam_role_policy_attachment" "terraform_admin" {
  role       = aws_iam_role.terraform.id
  policy_arn = data.aws_iam_policy.administrator_access.arn
}

resource "aws_iam_role" "image_publisher" {
  name               = "${var.project}-github-actions-image-publisher"
  assume_role_policy = data.aws_iam_policy_document.image_publisher_assume.json
  description        = "Assumed by GitHub Actions to push container images to ECR."

  tags = merge(var.tags, {
    Name      = "${var.project}-github-actions-image-publisher"
    Component = "ci_cd"
  })
}

resource "aws_iam_role_policy" "image_publisher" {
  name   = "image-publisher-inline"
  role   = aws_iam_role.image_publisher.id
  policy = data.aws_iam_policy_document.image_publisher_inline.json
}

