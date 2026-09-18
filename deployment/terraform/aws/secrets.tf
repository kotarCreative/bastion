resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db" {
  name_prefix             = "${local.name}-db-"
  description             = "PostgreSQL DSN for Bastion control-plane storage."
  recovery_window_in_days = 7
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  # The runtime DSN path is ignored (Bastion scopes to a per-root database).
  # The admin DSN is derived from the same base string.
  secret_string = "postgres://${aws_db_instance.main.username}:${random_password.db.result}@${aws_db_instance.main.endpoint}/postgres"
}
