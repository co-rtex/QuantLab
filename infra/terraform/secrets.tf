# Secrets Manager entries the tasks read at start. Terraform creates the containers only,
# never a version, so no secret value exists in code or state. An operator sets each value
# once before the first deploy (README.md, "First deployment"). ECS refuses to start a task
# whose secret has no value, so the optional ones are injected only when enabled
# (var.llm_enabled, var.market_data_provider), and must then be set first.

locals {
  secrets = {
    jwt_secret    = { env = "QUANTLAB_JWT_SECRET", required = true, desc = "JWT signing key, 32+ random characters" }
    metrics_token = { env = "QUANTLAB_METRICS_TOKEN", required = true, desc = "Bearer token for /metrics, 16+ characters" }
    edge_secret   = { env = "QUANTLAB_EDGE_SECRET", required = true, desc = "Shared edge-to-web secret, 32+ characters (ADR-0008)" }
    database_url  = { env = "QUANTLAB_DATABASE_URL", required = true, desc = "postgresql+psycopg URL of the application role, sslmode=require" }
    anthropic_key = { env = "QUANTLAB_ANTHROPIC_API_KEY", required = false, desc = "Optional LLM provider key" }
    market_data_key = {
      env      = "QUANTLAB_MARKET_DATA_API_KEY"
      required = false
      desc     = "Optional licensed market-data key; the vendor and licence are the user's choice (#11)"
    }
  }
}

resource "aws_secretsmanager_secret" "app" {
  #checkov:skip=CKV2_AWS_57:Application secrets are rotated by an operator (RUNBOOK); no rotation function exists for them
  for_each                = local.secrets
  name                    = "quantlab/${var.environment}/${each.key}"
  description             = "${each.value.desc} (${each.value.env})"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = 30
}
