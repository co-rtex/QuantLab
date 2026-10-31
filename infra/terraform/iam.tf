data "aws_iam_policy_document" "assume_ecs_tasks" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# Execution role: what ECS needs to start a task (pull the image, read its secrets,
# write its logs). The application never holds these permissions.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for s in aws_secretsmanager_secret.app : s.arn]
  }
  statement {
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "read-quantlab-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# Task role: what the running application may do. Only mount the data file system.
resource "aws_iam_role" "task" {
  name               = "${local.name}-task"
  assume_role_policy = data.aws_iam_policy_document.assume_ecs_tasks.json
}

data "aws_iam_policy_document" "task" {
  statement {
    actions   = ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"]
    resources = [aws_efs_file_system.data.arn]
    condition {
      test     = "StringEquals"
      variable = "elasticfilesystem:AccessPointArn"
      values   = [aws_efs_access_point.data.arn]
    }
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "mount-data"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}

# --- service roles ----------------------------------------------------------------------
data "aws_iam_policy_document" "assume_monitoring" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${local.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.assume_monitoring.json
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

data "aws_iam_policy_document" "assume_backup" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name               = "${local.name}-backup"
  assume_role_policy = data.aws_iam_policy_document.assume_backup.json
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

data "aws_iam_policy_document" "assume_flow_logs" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${local.name}-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.assume_flow_logs.json
}

data "aws_iam_policy_document" "flow_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
    resources = ["${aws_cloudwatch_log_group.flow.arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs.json
}

# --- container images -------------------------------------------------------------------
resource "aws_ecr_repository" "image" {
  for_each             = toset(["api", "web", "edge"])
  name                 = "${local.name}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}
