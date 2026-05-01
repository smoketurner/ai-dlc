################################################################################
# Roles assumed by GitHub Actions:
#   * terraform        — plan on PR, apply on push to main.
#   * image_publisher  — push images to ECR after build.
################################################################################

resource "aws_iam_role" "terraform" {
  name               = "${var.project}-github-actions-terraform"
  assume_role_policy = data.aws_iam_policy_document.terraform_assume.json
  description        = "Assumed by GitHub Actions for terraform plan/apply."
}

resource "aws_iam_role_policy" "terraform_inline" {
  name   = "terraform-inline"
  role   = aws_iam_role.terraform.id
  policy = data.aws_iam_policy_document.terraform_inline.json
}

# Power-user write access for apply. Many AWS resources don't support
# tag-on-create conditions; consumers should narrow this further (or replace
# entirely) once the org's CI/CD posture stabilises.
resource "aws_iam_role_policy_attachment" "terraform_power" {
  role       = aws_iam_role.terraform.id
  policy_arn = "arn:aws:iam::aws:policy/PowerUserAccess"
}

resource "aws_iam_role" "image_publisher" {
  name               = "${var.project}-github-actions-image-publisher"
  assume_role_policy = data.aws_iam_policy_document.image_publisher_assume.json
  description        = "Assumed by GitHub Actions to push container images to ECR."
}

resource "aws_iam_role_policy" "image_publisher" {
  name   = "image-publisher-inline"
  role   = aws_iam_role.image_publisher.id
  policy = data.aws_iam_policy_document.image_publisher_inline.json
}
