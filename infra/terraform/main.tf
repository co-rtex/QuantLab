data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

# One customer-managed key for this deployment's data at rest: RDS, its master-password
# secret, Redis, EFS, S3, Secrets Manager and CloudWatch Logs.
resource "aws_kms_key" "main" {
  description             = "QuantLab ${var.environment} data at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy                  = data.aws_iam_policy_document.kms.json
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.main.key_id
}

data "aws_iam_policy_document" "kms" {
  #checkov:skip=CKV_AWS_111:A key policy's "*" resource means this key only
  #checkov:skip=CKV_AWS_356:A key policy's "*" resource means this key only
  #checkov:skip=CKV_AWS_109:The account root administers its own key (AWS default key policy)
  statement {
    sid       = "AccountAdministers"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }
  statement {
    sid       = "CloudWatchLogsEncrypts"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${var.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${data.aws_partition.current.partition}:logs:${var.region}:${data.aws_caller_identity.current.account_id}:log-group:/quantlab/${var.environment}/*"]
    }
  }
  statement {
    sid       = "LoadBalancerAccessLogsEncrypt"
    actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}
