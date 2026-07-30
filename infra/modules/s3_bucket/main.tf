# infra/modules/s3_bucket — reusable bucket with versioning, SSE-KMS,
# a fully-blocking public access block, and configurable lifecycle rules.
# Used for all four data buckets (raw, processed, artifacts, data-capture);
# only the lifecycle window differs per caller.

resource "aws_s3_bucket" "this" {
  bucket        = var.name
  force_destroy = var.force_destroy

  tags = var.tags
}

resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "this" {
  bucket                  = aws_s3_bucket.this.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "this" {
  # Versioning must exist before a lifecycle config referencing noncurrent
  # version rules is applied.
  depends_on = [aws_s3_bucket_versioning.this]

  bucket = aws_s3_bucket.this.id

  rule {
    id     = "default"
    status = "Enabled"

    filter {}

    dynamic "expiration" {
      for_each = var.expiration_days > 0 ? [1] : []
      content {
        days = var.expiration_days
      }
    }

    dynamic "transition" {
      for_each = var.transitions
      content {
        days          = transition.value.days
        storage_class = transition.value.storage_class
      }
    }

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = var.abort_incomplete_multipart_upload_days
    }
  }
}
