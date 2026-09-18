output "bastion_url" {
  description = "URL to reach the deployed Bastion service."
  value       = var.acm_certificate_arn != "" ? "https://${local.public_host}" : "http://${local.public_host}"
}

output "load_balancer_dns" {
  description = "DNS name of the ALB."
  value       = aws_lb.main.dns_name
}

output "ecr_repository_url" {
  description = "URL of the ECR repository. Push the container image here before starting the service."
  value       = aws_ecr_repository.main.repository_url
}

output "database_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the PostgreSQL DSN."
  value       = aws_secretsmanager_secret.db.arn
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
  description = "Name of the ECS service."
  value       = aws_ecs_service.main.name
}

output "ecs_task_definition_family" {
  description = "Family of the main ECS task definition, used for one-off bootstrap tasks."
  value       = aws_ecs_task_definition.main.family
}

output "bootstrap_command" {
  description = "Example AWS CLI command to run the demo bootstrap one-off task."
  value       = "bash ${path.module}/init-forge.sh ${aws_ecs_cluster.main.name} ${aws_ecs_task_definition.main.family} '${join(",", aws_subnet.public[*].id)}' ${aws_security_group.ecs.id} ${aws_cloudwatch_log_group.main.name} ${var.aws_region}"
}

output "next_steps" {
  description = "What to do after Terraform apply."
  value       = <<EOT
1. Build and push the container image to ${aws_ecr_repository.main.repository_url}:latest
2. Run the bootstrap task: ${path.module}/init-forge.sh
3. Wait for the ECS service to report healthy.
4. Run the smoke test with the printed tokens:
   BASTION_URL=<bastion_url> BASTION_INSECURE_TLS=0 ${path.module}/smoke-test.sh
EOT
}
