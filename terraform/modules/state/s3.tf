################################################################################
# S3 buckets for run artifacts and per-project MEMORY.md snapshots.
#
# Hardening (both buckets): BPA, BucketOwnerEnforced, KMS-SSE, TLS-only,
# deny unencrypted PUTs, versioning, lifecycle. The artifacts bucket emits
# events to EventBridge so writes can be observed by the projector Lambda.
################################################################################

resource "aws_s3_bucket" "artifacts" {
  bucket = local.artifacts_name
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.s3_kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.artifacts_noncurrent_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_notification" "artifacts" {
  bucket      = aws_s3_bucket.artifacts.id
  eventbridge = true
}

resource "aws_s3_bucket_policy" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  policy = data.aws_iam_policy_document.bucket_baseline_artifacts.json
}

resource "aws_s3_bucket" "memory_md" {
  bucket = local.memory_md_name
}

resource "aws_s3_bucket_ownership_controls" "memory_md" {
  bucket = aws_s3_bucket.memory_md.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "memory_md" {
  bucket                  = aws_s3_bucket.memory_md.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "memory_md" {
  bucket = aws_s3_bucket.memory_md.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "memory_md" {
  bucket = aws_s3_bucket.memory_md.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.s3_kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "memory_md" {
  bucket = aws_s3_bucket.memory_md.id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.memory_md_noncurrent_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "memory_md" {
  bucket = aws_s3_bucket.memory_md.id
  policy = data.aws_iam_policy_document.bucket_baseline_memory_md.json
}
