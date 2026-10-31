# QUANTLAB_DATA_DIR on EFS, backups to AWS Backup and S3 (ADR-0012).
#
# The data directory needs POSIX semantics that object storage lacks: a dataset version
# is published by an atomic directory rename (store.write), reports by a hard link
# (write_report), #97's quarantine renames into $QUANTLAB_DATA_DIR/.trash, and the hourly
# sweep markers are file mtimes. EFS (NFSv4.1) provides all of them on one file system,
# so .trash shares a mount with datasets/ and artifacts/ as #97 requires. S3 holds
# off-site exports only (docs/RUNBOOK.md, "Backup and restore").

resource "aws_efs_file_system" "data" {
  creation_token   = "${local.name}-data"
  encrypted        = true
  kms_key_id       = aws_kms_key.main.arn
  performance_mode = "generalPurpose"
  throughput_mode  = "elastic"

  lifecycle_policy {
    transition_to_ia = "AFTER_30_DAYS"
  }
  lifecycle_policy {
    transition_to_primary_storage_class = "AFTER_1_ACCESS"
  }
  tags = { Name = "${local.name}-data" }
}

resource "aws_efs_mount_target" "data" {
  count           = var.az_count
  file_system_id  = aws_efs_file_system.data.id
  subnet_id       = aws_subnet.data[count.index].id
  security_groups = [aws_security_group.tier["efs"].id]
}

# The API image runs as uid 10001 (services/api/Dockerfile); every task mounts the same
# root through this access point, so ownership never depends on which task wrote a file.
resource "aws_efs_access_point" "data" {
  file_system_id = aws_efs_file_system.data.id
  posix_user {
    uid = 10001
    gid = 10001
  }
  root_directory {
    path = "/quantlab"
    creation_info {
      owner_uid   = 10001
      owner_gid   = 10001
      permissions = "0750"
    }
  }
}

resource "aws_efs_file_system_policy" "data" {
  file_system_id = aws_efs_file_system.data.id
  policy         = data.aws_iam_policy_document.efs.json
}

data "aws_iam_policy_document" "efs" {
  statement {
    sid     = "TasksMountThroughTheAccessPointOverTLS"
    actions = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.task.arn]
    }
    resources = [aws_efs_file_system.data.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.data.arn]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["true"]
    }
  }
}

# Daily recovery points of the data directory. Restore with the reaper stopped
# (docs/RUNBOOK.md): restored files keep their old mtimes.
resource "aws_efs_backup_policy" "data" {
  file_system_id = aws_efs_file_system.data.id
  backup_policy {
    status = "ENABLED"
  }
}

resource "aws_backup_vault" "main" {
  name        = local.name
  kms_key_arn = aws_kms_key.main.arn
}

resource "aws_backup_plan" "data" {
  name = "${local.name}-data"
  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 5 * * ? *)"
    lifecycle {
      delete_after = var.backup_retention_days
    }
  }
}

resource "aws_backup_selection" "data" {
  name         = "${local.name}-data"
  plan_id      = aws_backup_plan.data.id
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [aws_efs_file_system.data.arn]
}

# --- S3: off-site exports (pg_dump archives, data-directory snapshots) ------------------
resource "aws_s3_bucket" "exports" {
  #checkov:skip=CKV_AWS_18:Server access logs would need a second bucket; CloudTrail data events cover audits
  #checkov:skip=CKV_AWS_144:Cross-region replication is a per-deployment choice (README, "Disaster recovery")
  #checkov:skip=CKV2_AWS_62:Nothing consumes object events for exports
  bucket_prefix = "${local.name}-exports-"
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "exports" {
  bucket                  = aws_s3_bucket.exports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "exports" {
  bucket = aws_s3_bucket.exports.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "exports" {
  bucket = aws_s3_bucket.exports.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_bucket_policy" "exports" {
  bucket = aws_s3_bucket.exports.id
  policy = data.aws_iam_policy_document.exports.json
}

data "aws_iam_policy_document" "exports" {
  # NLB access logs (edge.tf) are delivered under nlb/.
  statement {
    sid       = "LoadBalancerAccessLogs"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.exports.arn}/nlb/AWSLogs/${data.aws_caller_identity.current.account_id}/*"]
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
    condition {
      test     = "StringEquals"
      variable = "s3:x-amz-acl"
      values   = ["bucket-owner-full-control"]
    }
  }
  statement {
    sid       = "LoadBalancerAccessLogsAcl"
    actions   = ["s3:GetBucketAcl"]
    resources = [aws_s3_bucket.exports.arn]
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
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.exports.arn, "${aws_s3_bucket.exports.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}
