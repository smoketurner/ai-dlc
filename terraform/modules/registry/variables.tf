variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "repositories" {
  description = "Repositories to create. Each is named `<project>/<key>`."
  type        = set(string)
  default = [
    "architect",
    "code_critic",
    "implementer",
    "retrospector",
    "reviewer",
    "tester",
    "proposer",
    "triage",
  ]
}

variable "agentcore_pull_repositories" {
  description = "Subset of repositories that AgentCore Runtime is allowed to pull."
  type        = set(string)
  default = [
    "architect",
    "code_critic",
    "implementer",
    "retrospector",
    "reviewer",
    "tester",
    "proposer",
    "triage",
  ]
}

variable "untagged_image_retention_days" {
  description = "Untagged images expire after this many days."
  type        = number
  default     = 1
}

variable "tagged_image_retention_count" {
  description = "Keep this many tagged images per repository."
  type        = number
  default     = 5
}

variable "tags" {
  description = "Additional tags applied to every taggable resource."
  type        = map(string)
  default     = {}
}
