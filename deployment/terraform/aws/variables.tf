variable "name_prefix" {
  description = "Prefix used for all named resources."
  type        = string
  default     = "bastion"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "dev"
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

variable "http_ingress_cidrs" {
  description = "Operator IPv4 /32 addresses allowed to use HTTP when ACM is not configured."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.http_ingress_cidrs :
      can(cidrhost(cidr, 0)) && !strcontains(cidr, ":") && endswith(cidr, "/32")
    ])
    error_message = "Unencrypted HTTP ingress must contain only individual IPv4 /32 operator addresses."
  }
}

variable "container_image" {
  description = "Digest-qualified container image URI. Leave empty while provisioning the foundation."
  type        = string
  default     = ""

  validation {
    condition     = var.container_image == "" || can(regex("@sha256:[0-9a-f]{64}$", var.container_image))
    error_message = "container_image must be empty or digest-qualified (repository@sha256:...)."
  }
}

variable "bootstrap_container_image" {
  description = "Digest-qualified image for the one-off bootstrap task. Defaults to container_image."
  type        = string
  default     = ""

  validation {
    condition     = var.bootstrap_container_image == "" || can(regex("@sha256:[0-9a-f]{64}$", var.bootstrap_container_image))
    error_message = "bootstrap_container_image must be empty or digest-qualified (repository@sha256:...)."
  }
}

variable "deploy_service" {
  description = "Create the long-lived ECS service after the image has been pushed and bootstrap has completed."
  type        = bool
  default     = false
}

variable "permissions_boundary_arn" {
  description = "Bootstrap-owned IAM permissions boundary required on every application task role."
  type        = string

  validation {
    condition     = can(regex("^arn:[^:]+:iam::[0-9]{12}:policy/.+$", var.permissions_boundary_arn))
    error_message = "permissions_boundary_arn must be an IAM policy ARN."
  }
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
  default     = "16"
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
  default     = "app"
  validation {
    condition = (
      can(regex("^[A-Za-z0-9!_.*'()/-]+$", var.s3_object_prefix)) &&
      !startswith(var.s3_object_prefix, "/") &&
      !endswith(var.s3_object_prefix, "/") &&
      split("/", var.s3_object_prefix)[0] != "qualification" &&
      alltrue([
        for component in split("/", var.s3_object_prefix) :
        !contains(["", ".", ".."], component)
      ])
    )
    error_message = "S3 object prefix must be a nonempty safe relative prefix outside qualification/."
  }
}
