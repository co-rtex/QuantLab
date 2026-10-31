# Secrets Manager entries the tasks read at start. Terraform creates the containers and
# sets no value, except redis_url (below, write-only), so no secret value exists in code
# or state. An operator sets each other value
# once before the first deploy (README.md, "First deployment"). ECS refuses to start a task
# whose secret has no value, so the optional ones are injected only when enabled
# (var.llm_enabled, var.market_data_provider), and must then be set first.

locals {
  secrets = {
    jwt_secret    = { env = "QUANTLAB_JWT_SECRET", required = true, desc = "JWT signing key, 32+ random characters" }
    metrics_token = { env = "QUANTLAB_METRICS_TOKEN", required = true, desc = "Bearer token for /metrics, 16+ characters" }
    edge_secret   = { env = "QUANTLAB_EDGE_SECRET", required = true, desc = "Shared edge-to-web secret, 32+ characters (ADR-0008)" }
    database_url  = { env = "QUANTLAB_DATABASE_URL", required = true, desc = "postgresql+psycopg URL of the application role, sslmode=verify-full with the RDS bundle" }
    anthropic_key = { env = "QUANTLAB_ANTHROPIC_API_KEY", required = false, desc = "Optional LLM provider key" }
    market_data_key = {
      env      = "QUANTLAB_MARKET_DATA_API_KEY"
      required = false
      desc     = "Optional licensed market-data key; the vendor and licence are the user's choice (#11)"
    }
  }
}

# QUANTLAB_REDIS_URL with the AUTH token, written through the write-only argument in the
# same apply that sets the token on the replication group (database.tf).
resource "aws_secretsmanager_secret" "redis_url" {
  #checkov:skip=CKV2_AWS_57:Rotated with the Redis token by bumping redis_auth_token_version
  name                    = "quantlab/${var.environment}/redis_url"
  description             = "rediss:// URL with the ElastiCache AUTH token (QUANTLAB_REDIS_URL)"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id                = aws_secretsmanager_secret.redis_url.id
  secret_string_wo         = "rediss://:${ephemeral.aws_secretsmanager_random_password.redis.random_password}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
  secret_string_wo_version = var.redis_auth_token_version
}

resource "aws_secretsmanager_secret" "app" {
  #checkov:skip=CKV2_AWS_57:Application secrets are rotated by an operator (RUNBOOK); no rotation function exists for them
  for_each                = local.secrets
  name                    = "quantlab/${var.environment}/${each.key}"
  description             = "${each.value.desc} (${each.value.env})"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 30
}
