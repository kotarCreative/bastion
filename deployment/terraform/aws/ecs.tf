resource "aws_ecs_cluster" "main" {
  name = local.name
  tags = local.tags
}

resource "aws_cloudwatch_log_group" "main" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7
  tags              = local.tags
}

resource "aws_ecs_task_definition" "bootstrap" {
  count = local.bootstrap_image != "" ? 1 : 0

  family                   = "${local.name}-bootstrap"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.ecs_task_cpu
  memory                   = var.ecs_task_memory
  execution_role_arn       = aws_iam_role.bootstrap_execution.arn
  task_role_arn            = aws_iam_role.bootstrap_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = templatefile("${path.module}/bootstrap_container_definition.json.tftpl", {
    image                       = local.bootstrap_image
    region                      = var.aws_region
    log_group                   = aws_cloudwatch_log_group.main.name
    host                        = local.public_host
    runtime_database_secret_arn = aws_secretsmanager_secret.db_runtime.arn
    admin_database_secret_arn   = aws_secretsmanager_secret.db_admin.arn
    demo_accounts_secret_arn    = aws_secretsmanager_secret.demo_accounts.arn
    bootstrap_tokens_secret_arn = aws_secretsmanager_secret.bootstrap_tokens.arn
    s3_bucket                   = aws_s3_bucket.repository.bucket
    s3_region                   = var.aws_region
    s3_expected_bucket_owner    = data.aws_caller_identity.current.account_id
    s3_object_prefix            = var.s3_object_prefix
  })

  volume {
    name = "bastion-data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.main.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.main.id
        iam             = "DISABLED"
      }
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.bootstrap_execution_managed,
    aws_iam_role_policy.bootstrap_execution_secrets,
    aws_iam_role_policy.bootstrap_task,
    aws_secretsmanager_secret_version.db_admin,
    aws_secretsmanager_secret_version.db_runtime,
    aws_secretsmanager_secret_version.demo_accounts,
    time_sleep.s3_versioning_propagation,
  ]

  tags = local.tags
}

resource "aws_ecs_task_definition" "main" {
  count = var.deploy_service ? 1 : 0

  family                   = local.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.ecs_task_cpu
  memory                   = var.ecs_task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = templatefile("${path.module}/container_definitions.json.tftpl", {
    image                       = local.image
    region                      = var.aws_region
    log_group                   = aws_cloudwatch_log_group.main.name
    host                        = local.public_host
    runtime_database_secret_arn = aws_secretsmanager_secret.db_runtime.arn
    worker_interval_seconds     = var.worker_interval_seconds
    s3_bucket                   = aws_s3_bucket.repository.bucket
    s3_region                   = var.aws_region
    s3_expected_bucket_owner    = data.aws_caller_identity.current.account_id
    s3_object_prefix            = var.s3_object_prefix
  })

  volume {
    name = "bastion-data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.main.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.main.id
        iam             = "DISABLED"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = can(regex("@sha256:[0-9a-f]{64}$", var.container_image))
      error_message = "deploy_service requires a digest-qualified container_image."
    }
  }

  tags = local.tags
}

resource "aws_ecs_service" "main" {
  count = var.deploy_service ? 1 : 0

  name             = local.name
  cluster          = aws_ecs_cluster.main.id
  task_definition  = aws_ecs_task_definition.main[0].arn
  desired_count    = 1
  launch_type      = "FARGATE"
  platform_version = "1.4.0"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.main.arn
    container_name   = "bastion-web"
    container_port   = 8000
  }

  depends_on = [
    aws_lb_listener.http_forward,
    aws_lb_listener.http_redirect,
    aws_lb_listener.https,
    aws_iam_role_policy_attachment.execution_managed,
    aws_iam_role_policy.execution_secrets,
    aws_iam_role_policy.task,
    time_sleep.s3_versioning_propagation,
  ]

  propagate_tags = "SERVICE"
  tags           = local.tags
}
