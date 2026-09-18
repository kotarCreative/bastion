variable "name_prefix" {
  description = "Prefix used for all named resources."
  type        = string
  default     = "bastion"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "test"
}

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidrs" {
  description = "Public subnet CIDRs (one per AZ)."
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24"]
}

variable "private_subnet_cidrs" {
  description = "Private subnet CIDRs for RDS (one per AZ)."
  type        = list(string)
  default     = ["10.0.101.0/24", "10.0.102.0/24"]
}

variable "availability_zones" {
  description = "Explicit AZs to use. Leave empty to discover from the region."
  type        = list(string)
  default     = []
}

variable "domain_name" {
  description = "Public DNS name for the service. Optional."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone ID for domain_name. Optional."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "ACM certificate ARN for HTTPS. Optional."
  type        = string
  default     = ""
}

variable "container_image" {
  description = "Container image URI. Leave empty to use the ECR repo URL with the 'latest' tag."
  type        = string
  default     = ""
}

variable "bastion_host" {
  description = "External hostname used by Bastion. Defaults to domain_name or the ALB DNS name."
  type        = string
  default     = ""
}

variable "worker_interval_seconds" {
  description = "Seconds between projection worker passes."
  type        = number
  default     = 30
}

variable "enable_demo_bootstrap" {
  description = "Whether to create a one-off bootstrap task definition for the demo catalog."
  type        = bool
  default     = true
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GiB."
  type        = number
  default     = 20
}

variable "db_engine_version" {
  description = "RDS PostgreSQL engine version."
  type        = string
  default     = "16.4"
}

variable "ecs_task_cpu" {
  description = "Fargate task CPU units."
  type        = number
  default     = 1024
}

variable "ecs_task_memory" {
  description = "Fargate task memory in MiB."
  type        = number
  default     = 2048
}

variable "demo_tester_password" {
  description = "Password for the demo tester account."
  type        = string
  default     = "bastion-tester"
}

variable "demo_observer_password" {
  description = "Password for the demo observer account."
  type        = string
  default     = "bastion-observer"
}

variable "s3_noncurrent_version_retention_days" {
  description = "Days to retain noncurrent S3 versions for operator recovery."
  type        = number
  default     = 30
  validation {
    condition     = var.s3_noncurrent_version_retention_days >= 1
    error_message = "S3 noncurrent version retention must be positive."
  }
}

variable "s3_object_prefix" {
  description = "Deployment prefix used by the Bastion S3 repository."
  type        = string
  default     = ""
  validation {
    condition = var.s3_object_prefix == "" || (
      can(regex("^[A-Za-z0-9!_.*'()/-]+$", var.s3_object_prefix)) &&
      !startswith(var.s3_object_prefix, "/") &&
      !endswith(var.s3_object_prefix, "/") &&
      !strcontains(var.s3_object_prefix, "//") &&
      !strcontains(var.s3_object_prefix, "/./") &&
      !strcontains(var.s3_object_prefix, "/../")
    )
    error_message = "S3 object prefix must be a relative ASCII key prefix without unsafe components."
  }
}
