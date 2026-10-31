# --- PostgreSQL (RDS) with point-in-time recovery --------------------------------------
resource "aws_db_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.data[*].id
}

resource "aws_db_parameter_group" "main" {
  name   = "${local.name}-pg17"
  family = "postgres17"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = "17"
  instance_class = var.db_instance_class
  db_name        = "quantlab"
  username       = "quantlab_admin"
  # RDS creates and rotates the master password in Secrets Manager: no password in
  # code or state. The application uses its own role (README, "First deployment").
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.main.arn

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.main.arn

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.tier["db"].id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  # Point-in-time recovery to any second within the retention window.
  backup_retention_period   = var.db_backup_retention_days
  backup_window             = "03:00-04:00"
  maintenance_window        = "sun:04:30-sun:05:30"
  copy_tags_to_snapshot     = true
  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-final"
  delete_automated_backups  = false

  auto_minor_version_upgrade          = true
  iam_database_authentication_enabled = true
  performance_insights_enabled        = true
  performance_insights_kms_key_id     = aws_kms_key.main.arn
  monitoring_interval                 = 60
  monitoring_role_arn                 = aws_iam_role.rds_monitoring.arn
  enabled_cloudwatch_logs_exports     = ["postgresql", "upgrade"]
}

# --- Redis (ElastiCache): queue delivery and caches only (ADR-0002) --------------------
resource "aws_elasticache_subnet_group" "main" {
  name       = local.name
  subnet_ids = aws_subnet.data[*].id
}

resource "aws_elasticache_replication_group" "main" {
  #checkov:skip=CKV_AWS_31:An AUTH token would sit in Terraform state; network isolation instead (ADR-0012)
  replication_group_id = local.name
  description          = "QuantLab ${var.environment} queue delivery and caches"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  num_cache_clusters   = 2
  port                 = 6379

  automatic_failover_enabled = true
  multi_az_enabled           = true
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.tier["redis"].id]

  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.main.arn
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  # No AUTH token: one would have to be written into Terraform state. Redis holds only
  # job ids for delivery and caches; PostgreSQL decides which job runs (ADR-0002), and
  # only the api, worker and reaper groups can reach it (ADR-0012).

  snapshot_retention_limit   = 1
  auto_minor_version_upgrade = true
  apply_immediately          = false
}
