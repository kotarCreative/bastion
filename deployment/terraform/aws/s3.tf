data "aws_caller_identity" "current" {}

resource "random_id" "s3_bucket" {
  byte_length = 6
}

resource "aws_s3_bucket" "repository" {
  bucket        = "${local.name}-${data.aws_caller_identity.current.account_id}-${random_id.s3_bucket.hex}"
  force_destroy = true
  tags          = local.tags
}

resource "aws_s3_bucket_versioning" "repository" {
  bucket = aws_s3_bucket.repository.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "time_sleep" "s3_versioning_propagation" {
  create_duration = "15m"
  depends_on      = [aws_s3_bucket_versioning.repository]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "repository" {
  bucket = aws_s3_bucket.repository.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "repository" {
  bucket = aws_s3_bucket.repository.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_public_access_block" "repository" {
  bucket                  = aws_s3_bucket.repository.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "repository" {
  bucket = aws_s3_bucket.repository.id
  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.s3_noncurrent_version_retention_days
    }
    expiration { expired_object_delete_marker = true }
  }
  rule {
    id     = "expire-qualification-runs"
    status = "Enabled"
    filter { prefix = "qualification/" }
    expiration { days = 7 }
    noncurrent_version_expiration { noncurrent_days = 7 }
  }
}

resource "aws_s3_bucket_policy" "repository" {
  bucket = aws_s3_bucket.repository.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.repository.arn, "${aws_s3_bucket.repository.arn}/*"]
        Condition = {
          Bool = {
            "aws:SecureTransport"       = "false"
            "aws:PrincipalIsAWSService" = "false"
          }
        }
      },
      {
        Sid       = "DenyUnqualifiedPut"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.repository.arn}/*"
        Condition = { Null = { "s3:if-match" = "true", "s3:if-none-match" = "true" } }
      },
      {
        Sid       = "DenyUnqualifiedDelete"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:DeleteObject"
        Resource  = "${aws_s3_bucket.repository.arn}/*"
        Condition = { Null = { "s3:if-match" = "true" } }
      },
      {
        Sid       = "DenyNonSseS3Writes"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.repository.arn}/*"
        Condition = { StringNotEquals = { "s3:x-amz-server-side-encryption" = "AES256" } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.repository]
}
