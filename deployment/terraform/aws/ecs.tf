resource "aws_ecs_cluster" "main" {
  name = local.name
  tags = local.tags
}

resource "aws_cloudwatch_log_group" "main" {
  name              = "/ecs/${local.name}"
  retention_in_days = 7
  tags              = local.tags
}

resource "aws_ecs_task_definition" "main" {
  family                   = local.name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.ecs_task_cpu
  memory                   = var.ecs_task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = templatefile("${path.module}/container_definitions.json.tftpl", {
    image                   = local.image
    region                  = var.aws_region
    log_group               = aws_cloudwatch_log_group.main.name
    host                    = local.public_host
    database_secret_arn     = aws_secretsmanager_secret.db.arn
    admin_database_secret_arn = aws_secretsmanager_secret.db.arn
    tester_password         = var.demo_tester_password
    observer_password       = var.demo_observer_password
    worker_interval_seconds = var.worker_interval_seconds
    s3_bucket               = aws_s3_bucket.repository.bucket
    s3_region               = var.aws_region
    s3_expected_bucket_owner = data.aws_caller_identity.current.account_id
    s3_object_prefix        = var.s3_object_prefix
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

  tags = local.tags
}

resource "aws_ecs_service" "main" {
  name            = local.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.main.arn
  desired_count   = 1
  launch_type     = "FARGATE"

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
    time_sleep.s3_versioning_propagation,
  ]

  propagate_tags = "SERVICE"
  tags           = local.tags
}
