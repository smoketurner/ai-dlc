variable "project" {
  description = "Project name."
  type        = string
  default     = "ai-dlc"
}

variable "env" {
  description = "Environment name."
  type        = string
}

variable "s3_kms_key_arn" {
  description = "KMS key ARN for SSE on the S3 buckets."
  type        = string
}

variable "ddb_kms_key_arn" {
  description = "KMS key ARN for SSE on the DynamoDB tables."
  type        = string
}

variable "artifacts_noncurrent_expiration_days" {
  description = "Lifecycle expiration for noncurrent versions in the artifacts bucket."
  type        = number
  default     = 365
}

variable "memory_md_noncurrent_expiration_days" {
  description = "Lifecycle expiration for noncurrent versions in the memory_md bucket."
  type        = number
  default     = 90
}
