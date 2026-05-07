output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "artifacts_bucket_arn" {
  value = aws_s3_bucket.artifacts.arn
}

output "memory_md_bucket" {
  value = aws_s3_bucket.memory_md.id
}

output "memory_md_bucket_arn" {
  value = aws_s3_bucket.memory_md.arn
}

output "runs_table" {
  value = aws_dynamodb_table.runs.name
}

output "runs_table_arn" {
  value = aws_dynamodb_table.runs.arn
}

output "runs_stream_arn" {
  value = aws_dynamodb_table.runs.stream_arn
}

output "idempotency_table" {
  value = aws_dynamodb_table.idempotency_keys.name
}

output "idempotency_table_arn" {
  value = aws_dynamodb_table.idempotency_keys.arn
}
