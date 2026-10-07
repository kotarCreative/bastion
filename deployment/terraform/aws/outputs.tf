output "bastion_url" {
  description = "URL to reach the deployed Bastion service after deploy_service is enabled."
  value       = var.deploy_service ? (var.acm_certificate_arn != "" ? "https://${local.public_host}" : "http://${local.public_host}") : null
}

output "load_balancer_dns" {
  description = "DNS name of the ALB."
  value       = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  description = "Repository where the deployment script publishes immutable images."
  value       = aws_ecr_repository.main.repository_url
}

output "ecr_repository_name" {
  description = "ECR repository name used to resolve an image tag to its digest."
  value       = aws_ecr_repository.main.name
}

output "runtime_database_secret_arn" {
  description = "Restricted PostgreSQL runtime DSN secret ARN."
  value       = aws_secretsmanager_secret.db_runtime.arn
}

output "bootstrap_tokens_secret_arn" {
  description = "Secret populated by bootstrap with tester and observer API/Git tokens."
  value       = aws_secretsmanager_secret.bootstrap_tokens.arn
}

output "repository_bucket" {
  description = "Dedicated S3 bucket used for repository truth."
  value       = aws_s3_bucket.repository.bucket
}

output "repository_bucket_owner" {
  description = "Expected AWS account ID for the repository bucket."
  value       = data.aws_caller_identity.current.account_id
}

output "ecs_cluster_name" {
  description = "Name of the ECS cluster."
  value       = aws_ecs_cluster.main.name
}

output "ecs_service_name" {
  description = "Name of the ECS service when deploy_service is enabled."
  value       = var.deploy_service ? aws_ecs_service.main[0].name : null
}

output "bootstrap_task_definition_arn" {
  description = "Exact one-off bootstrap task revision, available after a bootstrap image is set."
  value       = local.bootstrap_image != "" ? aws_ecs_task_definition.bootstrap[0].arn : null
}

output "public_subnet_ids" {
  description = "Public subnets used by one-off and service tasks."
  value       = aws_subnet.public[*].id
}

output "ecs_security_group_id" {
  description = "Security group used by ECS tasks."
  value       = aws_security_group.ecs.id
}

output "cloudwatch_log_group" {
  description = "CloudWatch log group for bootstrap and service diagnostics."
  value       = aws_cloudwatch_log_group.main.name
}

output "aws_region" {
  description = "AWS region used by deployment scripts."
  value       = var.aws_region
}
