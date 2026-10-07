locals {
  ecs_assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  s3_runtime_statements = [
    {
      Effect = "Allow"
      Action = [
        "s3:GetBucketLocation",
        "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration",
      ]
      Resource = aws_s3_bucket.repository.arn
      Condition = {
        StringEquals = {
          "aws:ResourceAccount" = data.aws_caller_identity.current.account_id
        }
      }
    },
    {
      Effect   = "Allow"
      Action   = ["s3:ListBucket"]
      Resource = aws_s3_bucket.repository.arn
      Condition = {
        StringEquals = {
          "aws:ResourceAccount" = data.aws_caller_identity.current.account_id
        }
        StringLike = {
          "s3:prefix" = var.s3_object_prefix == "" ? ["*"] : [var.s3_object_prefix, "${var.s3_object_prefix}/*"]
        }
      }
    },
    {
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = "${aws_s3_bucket.repository.arn}/${var.s3_object_prefix == "" ? "" : "${var.s3_object_prefix}/"}*"
      Condition = {
        StringEquals = {
          "aws:ResourceAccount" = data.aws_caller_identity.current.account_id
        }
      }
    },
  ]
}

resource "aws_iam_role" "execution" {
  name                 = "${local.name}-execution"
  assume_role_policy   = local.ecs_assume_role_policy
  permissions_boundary = var.permissions_boundary_arn
  tags                 = local.tags
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "runtime-secret"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_secretsmanager_secret.db_runtime.arn]
    }]
  })
}

resource "aws_iam_role" "bootstrap_execution" {
  name                 = "${local.name}-bootstrap-execution"
  assume_role_policy   = local.ecs_assume_role_policy
  permissions_boundary = var.permissions_boundary_arn
  tags                 = local.tags
}

resource "aws_iam_role_policy_attachment" "bootstrap_execution_managed" {
  role       = aws_iam_role.bootstrap_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "bootstrap_execution_secrets" {
  name = "bootstrap-secrets"
  role = aws_iam_role.bootstrap_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["secretsmanager:GetSecretValue"]
      Resource = [
        aws_secretsmanager_secret.db_admin.arn,
        aws_secretsmanager_secret.db_runtime.arn,
      ]
    }]
  })
}

resource "aws_iam_role" "task" {
  name                 = "${local.name}-task"
  assume_role_policy   = local.ecs_assume_role_policy
  permissions_boundary = var.permissions_boundary_arn
  tags                 = local.tags
}

resource "aws_iam_role_policy" "task" {
  name = "repository"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = local.s3_runtime_statements
  })
}

resource "aws_iam_role" "bootstrap_task" {
  name                 = "${local.name}-bootstrap-task"
  assume_role_policy   = local.ecs_assume_role_policy
  permissions_boundary = var.permissions_boundary_arn
  tags                 = local.tags
}

resource "aws_iam_role_policy" "bootstrap_task" {
  name = "repository-and-token-secret"
  role = aws_iam_role.bootstrap_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.s3_runtime_statements, [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]
      Resource = aws_secretsmanager_secret.bootstrap_tokens.arn
    }])
  })
}
