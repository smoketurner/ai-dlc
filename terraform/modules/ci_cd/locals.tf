locals {
  repo                  = "${var.github_owner}/${var.github_repo}"
  pr_subject            = "repo:${local.repo}:pull_request"
  branch_subjects_tf    = [for b in var.terraform_role_branches : "repo:${local.repo}:ref:refs/heads/${b}"]
  branch_subjects_image = [for b in var.image_publisher_branches : "repo:${local.repo}:ref:refs/heads/${b}"]
  branch_subjects_evals = [for b in var.evals_role_branches : "repo:${local.repo}:ref:refs/heads/${b}"]
}
