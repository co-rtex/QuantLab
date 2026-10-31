# Containers on ECS Fargate: edge (Caddy), web (Next.js), api, worker, reaper, and a
# one-off migrate task. Mirrors docker-compose.yml with infra/edge/compose.yml.

locals {
  namespace   = "${var.environment}.quantlab.internal"
  image       = { for k, r in aws_ecr_repository.image : k => "${r.repository_url}:${var.image_tag}" }
  market_data = var.market_data_provider != ""

  python_env = concat(
    [
      { name = "QUANTLAB_ENVIRONMENT", value = "production" },
      { name = "QUANTLAB_COOKIE_SECURE", value = "true" },
      { name = "QUANTLAB_DATA_DIR", value = "/data" },
      # Only the web tier's subnets may state a client address (ADR-0008).
      { name = "QUANTLAB_TRUSTED_PROXIES", value = jsonencode(local.web_cidrs) },
      { name = "QUANTLAB_GIT_SHA", value = var.image_tag },
    ],
    local.market_data ? [
      { name = "QUANTLAB_MARKET_DATA_PROVIDER", value = var.market_data_provider },
      { name = "QUANTLAB_MARKET_DATA_LICENSE", value = var.market_data_license },
      { name = "QUANTLAB_MARKET_DATA_PLAN", value = var.market_data_plan },
    ] : [],
  )
  python_secrets = concat(
    [for k in ["database_url"] : {
      name      = local.secrets[k].env
      valueFrom = aws_secretsmanager_secret.app[k].arn
    }],
    [{ name = "QUANTLAB_REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn }],
    var.llm_enabled ? [{
      name      = local.secrets.anthropic_key.env
      valueFrom = aws_secretsmanager_secret.app["anthropic_key"].arn
    }] : [],
    local.market_data ? [{
      name      = local.secrets.market_data_key.env
      valueFrom = aws_secretsmanager_secret.app["market_data_key"].arn
    }] : [],
  )

  # Only the API signs tokens and serves /metrics (#117): worker, reaper and migrate
  # never receive these.
  api_secrets = [for k in ["jwt_secret", "metrics_token"] : {
    name      = local.secrets[k].env
    valueFrom = aws_secretsmanager_secret.app[k].arn
  }]

  data_volume_mount = { sourceVolume = "data", containerPath = "/data", readOnly = false }
  tmp_mount         = { sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }

  python = {
    api = {
      cpu     = 1024
      memory  = 2048
      count   = var.api_count
      port    = 8000
      command = ["sh", "-c", "rm -rf \"$PROMETHEUS_MULTIPROC_DIR\" && mkdir -p \"$PROMETHEUS_MULTIPROC_DIR\" && exec uvicorn quantlab_api.main:app --host 0.0.0.0 --port 8000 --proxy-headers --workers ${var.api_workers}"]
      env     = [{ name = "PROMETHEUS_MULTIPROC_DIR", value = "/tmp/prometheus" }]
    }
    worker = {
      cpu     = 1024
      memory  = 2048
      count   = var.worker_count
      port    = null
      command = ["quantlab-worker"]
      env     = []
    }
    # Exactly one reaper. It mounts the data directory: the upload and orphan sweeps and
    # the trash purge (#21, #97) act on it.
    reaper = {
      cpu     = 256
      memory  = 512
      count   = 1
      port    = null
      command = ["quantlab-cli", "reaper", "--interval", "15"]
      env     = []
    }
  }
}

resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "service" {
  for_each          = toset(["edge", "web", "api", "worker", "reaper", "migrate"])
  name              = "/quantlab/${var.environment}/${each.key}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_service_discovery_private_dns_namespace" "main" {
  name = local.namespace
  vpc  = aws_vpc.main.id
}

resource "aws_service_discovery_service" "internal" {
  for_each = toset(["api", "web"])
  name     = each.key
  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }
  health_check_custom_config {}
}

# --- Python tiers ------------------------------------------------------------------------
resource "aws_ecs_task_definition" "python" {
  for_each                 = local.python
  family                   = "${local.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  volume {
    name = "data"
    efs_volume_configuration {
      file_system_id     = aws_efs_file_system.data.id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = aws_efs_access_point.data.id
        iam             = "ENABLED"
      }
    }
  }
  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([{
    name                   = each.key
    image                  = local.image.api
    essential              = true
    command                = each.value.command
    readonlyRootFilesystem = true
    user                   = "10001"
    environment            = concat(local.python_env, each.value.env)
    secrets                = concat(local.python_secrets, each.key == "api" ? local.api_secrets : [])
    mountPoints            = [local.data_volume_mount, local.tmp_mount]
    portMappings           = each.value.port == null ? [] : [{ containerPort = each.value.port, protocol = "tcp" }]
    healthCheck = each.key == "api" ? {
      command     = ["CMD", "python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health').status==200 else 1)"]
      interval    = 10
      timeout     = 5
      retries     = 6
      startPeriod = 30
    } : null
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.service[each.key].name
        awslogs-region        = var.region
        awslogs-stream-prefix = each.key
      }
    }
  }])
}

resource "aws_ecs_service" "python" {
  for_each        = local.python
  name            = each.key
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.python[each.key].arn
  desired_count   = each.value.count
  launch_type     = "FARGATE"
  # The reaper is replaced, never doubled, during a deploy.
  deployment_maximum_percent         = each.key == "reaper" ? 100 : 200
  deployment_minimum_healthy_percent = each.key == "reaper" ? 0 : 100
  propagate_tags                     = "SERVICE"
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.app[*].id
    security_groups  = [aws_security_group.tier[each.key].id]
    assign_public_ip = false
  }

  dynamic "service_registries" {
    for_each = each.key == "api" ? [1] : []
    content {
      registry_arn = aws_service_discovery_service.internal["api"].arn
    }
  }
}

# Schema migrations run as a one-off task before a deploy (README.md), never at API start:
# several API tasks starting at once would race to migrate.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  volume {
    name = "tmp"
  }
  container_definitions = jsonencode([{
    name                   = "migrate"
    image                  = local.image.api
    essential              = true
    command                = ["alembic", "-c", "services/api/alembic.ini", "upgrade", "head"]
    readonlyRootFilesystem = true
    user                   = "10001"
    environment            = local.python_env
    secrets                = local.python_secrets
    mountPoints            = [local.tmp_mount]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.service["migrate"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "migrate"
      }
    }
  }])
}

# --- web tier -----------------------------------------------------------------------------
resource "aws_ecs_task_definition" "web" {
  family                   = "${local.name}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  volume {
    name = "tmp"
  }
  volume {
    name = "next-cache"
  }
  container_definitions = jsonencode([{
    name                   = "web"
    image                  = local.image.web
    essential              = true
    readonlyRootFilesystem = true
    environment = [
      { name = "API_INTERNAL_URL", value = "http://api.${local.namespace}:8000" },
      { name = "NODE_ENV", value = "production" },
    ]
    # With the secret set, the web tier refuses requests that did not come through the
    # edge, and forwards only the edge's client address (ADR-0008).
    secrets = [{
      name      = local.secrets.edge_secret.env
      valueFrom = aws_secretsmanager_secret.app["edge_secret"].arn
    }]
    portMappings = [{ containerPort = 3000, protocol = "tcp" }]
    mountPoints = [
      local.tmp_mount,
      { sourceVolume = "next-cache", containerPath = "/app/.next/cache", readOnly = false },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.service["web"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "web"
      }
    }
  }])
}

resource "aws_ecs_service" "web" {
  name                   = "web"
  cluster                = aws_ecs_cluster.main.id
  task_definition        = aws_ecs_task_definition.web.arn
  desired_count          = var.web_count
  launch_type            = "FARGATE"
  propagate_tags         = "SERVICE"
  enable_execute_command = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    # Its own subnets: the API trusts exactly these CIDRs to state client addresses.
    subnets          = aws_subnet.web[*].id
    security_groups  = [aws_security_group.tier["web"].id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.internal["web"].arn
  }
}
