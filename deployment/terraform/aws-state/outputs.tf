output "bucket_name" {
  description = "Bucket to place in the application backend configuration."
  value       = aws_s3_bucket.state.bucket
}

output "application_state_key" {
  description = "Object key used by the application Terraform state."
  value       = var.application_state_key
}

output "aws_account_id" {
  description = "AWS account containing the Bastion deployment."
  value       = data.aws_caller_identity.current.account_id
}

output "aws_region" {
  description = "AWS region used by the bootstrap and application roots."
  value       = var.aws_region
}

output "github_deploy_role_arn" {
  description = "Role assumed by the protected AWS deployment workflow."
  value       = aws_iam_role.github_deploy.arn
}

output "github_qualification_role_arn" {
  description = "Role assumed by the protected live-S3 qualification workflow."
  value       = aws_iam_role.github_qualification.arn
}

output "application_permissions_boundary_arn" {
  description = "Permissions boundary required on application-created IAM roles."
  value       = aws_iam_policy.application_boundary.arn
}
