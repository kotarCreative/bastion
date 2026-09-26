variable "aws_region" {
  description = "AWS region for the Terraform state bucket."
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name for Terraform state. Leave empty for the account-scoped default."
  type        = string
  default     = ""

  validation {
    condition     = var.bucket_name == "" || can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be empty or a valid S3 bucket name."
  }
}

variable "github_repository" {
  description = "GitHub repository allowed to assume deployment roles, in OWNER/REPOSITORY form."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must use OWNER/REPOSITORY form."
  }
}

variable "github_oidc_provider_arn" {
  description = "Existing account-wide GitHub OIDC provider ARN. Leave empty to create it."
  type        = string
  default     = ""
}

variable "github_oidc_thumbprint" {
  description = "SHA-1 thumbprint used only when creating the GitHub OIDC provider."
  type        = string
  default     = "6938fd4d98bab03faadb97b34396831e3780aea1"
}

variable "deploy_environment" {
  description = "Protected GitHub environment allowed to deploy the application."
  type        = string
  default     = "aws-dev"
}

variable "qualification_environment" {
  description = "Protected GitHub environment allowed to qualify live S3 behavior."
  type        = string
  default     = "aws-s3-qualification"
}

variable "application_name_prefix" {
  description = "Name prefix used by the application Terraform root."
  type        = string
  default     = "bastion"
}

variable "application_environment" {
  description = "Environment name used by the application Terraform root."
  type        = string
  default     = "dev"
}

variable "application_state_key" {
  description = "S3 object key used by the application Terraform backend."
  type        = string
  default     = "bastion/dev/terraform.tfstate"
}
