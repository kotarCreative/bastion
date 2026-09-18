locals {
  name = "${var.name_prefix}-${var.environment}"
  tags = {
    Project     = "bastion"
    Environment = var.environment
    ManagedBy   = "terraform"
  }

  public_host = var.bastion_host != "" ? var.bastion_host : (var.domain_name != "" ? var.domain_name : aws_lb.main.dns_name)
  image       = var.container_image != "" ? var.container_image : "${aws_ecr_repository.main.repository_url}:latest"
}
