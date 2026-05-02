variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "github_owner" {
  description = "GitHub org/user that owns the repository."
  type        = string
}

variable "github_repo" {
  description = "GitHub repository name."
  type        = string
}

variable "terraform_role_branches" {
  description = "Branches that may assume the terraform role for `apply`."
  type        = list(string)
  default     = ["main"]
}

variable "image_publisher_branches" {
  description = "Branches that may assume the image-publisher role."
  type        = list(string)
  default     = ["main"]
}

variable "evals_role_branches" {
  description = "Branches that may assume the evals role for `workflow_dispatch` runs."
  type        = list(string)
  default     = ["main"]
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
