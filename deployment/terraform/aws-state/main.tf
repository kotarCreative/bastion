data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  application_name = "${var.application_name_prefix}-${var.application_environment}"
  bootstrap_name   = "${var.application_name_prefix}-bootstrap-${var.application_environment}"
  state_bucket_name = var.bucket_name != "" ? var.bucket_name : join("-", [
    var.application_name_prefix,
    "terraform-state",
    data.aws_caller_identity.current.account_id,
    var.aws_region,
  ])
  tags = {
    Project   = "bastion"
    ManagedBy = "terraform-bootstrap"
  }
}

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket_name

  tags = local.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource = [
        aws_s3_bucket.state.arn,
        "${aws_s3_bucket.state.arn}/*",
      ]
      Condition = {
        Bool = { "aws:SecureTransport" = "false" }
      }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}
