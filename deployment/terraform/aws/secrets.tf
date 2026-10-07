resource "random_password" "db_admin" {
  length  = 32
  special = false
}

resource "random_password" "db_runtime" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db_admin" {
  name_prefix             = "${local.name}-db-admin-"
  description             = "One-shot PostgreSQL administration DSN used by the bootstrap task."
  recovery_window_in_days = 7
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "db_admin" {
  secret_id     = aws_secretsmanager_secret.db_admin.id
  secret_string = "postgresql://bastion_admin:${random_password.db_admin.result}@${aws_db_instance.main.endpoint}/postgres?sslmode=require"
}

resource "aws_secretsmanager_secret" "db_runtime" {
  name_prefix             = "${local.name}-db-runtime-"
  description             = "Restricted PostgreSQL DSN used by Bastion services."
  recovery_window_in_days = 7
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "db_runtime" {
  secret_id     = aws_secretsmanager_secret.db_runtime.id
  secret_string = "postgresql://bastion_runtime:${random_password.db_runtime.result}@${aws_db_instance.main.endpoint}/postgres?sslmode=require"
}

resource "aws_secretsmanager_secret" "bootstrap_tokens" {
  name_prefix             = "${local.name}-bootstrap-tokens-"
  description             = "Git/API tokens written by the one-off bootstrap task."
  recovery_window_in_days = 7
  tags                    = local.tags
}
