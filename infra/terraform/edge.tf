# The TLS edge (ADR-0008, ADR-0012). A network load balancer terminates TLS with ACM and
# forwards TCP to Caddy with a PROXY protocol v2 header that names the client. Caddy
# accepts that header only from the NLB's subnets (edge/Caddyfile), so its view of the
# peer is the real client address. As in compose, it then overwrites X-Forwarded-For with
# it. No HTTP header from the client is ever parsed for an address: the source of truth
# is the PROXY header, which only the NLB can send (the edge accepts traffic only from
# the NLB's security group).

resource "aws_lb" "edge" {
  name                             = substr("${local.name}-edge", 0, 32)
  load_balancer_type               = "network"
  internal                         = false
  subnets                          = aws_subnet.public[*].id
  security_groups                  = [aws_security_group.tier["nlb"].id]
  enable_cross_zone_load_balancing = true
  enable_deletion_protection       = true

  access_logs {
    bucket  = aws_s3_bucket.exports.id
    prefix  = "nlb"
    enabled = true
  }
  depends_on = [aws_s3_bucket_policy.exports]
}

resource "aws_lb_target_group" "edge" {
  name                 = substr("${local.name}-edge", 0, 32)
  port                 = 8080
  protocol             = "TCP"
  target_type          = "ip"
  vpc_id               = aws_vpc.main.id
  proxy_protocol_v2    = true
  preserve_client_ip   = "false" # the PROXY header carries the address instead
  deregistration_delay = 30

  health_check {
    protocol            = "TCP"
    port                = "traffic-port"
    healthy_threshold   = 3
    unhealthy_threshold = 3
    interval            = 10
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.edge.arn
  port              = 443
  protocol          = "TLS"
  certificate_arn   = var.acm_certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  alpn_policy       = "HTTP1Only"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.edge.arn
  }
}

resource "aws_ecs_task_definition" "edge" {
  family                   = "${local.name}-edge"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }
  volume {
    name = "caddy-data"
  }
  volume {
    name = "caddy-config"
  }
  container_definitions = jsonencode([{
    name                   = "edge"
    image                  = local.image.edge
    essential              = true
    readonlyRootFilesystem = true
    environment = [
      { name = "QUANTLAB_EDGE_UPSTREAM", value = "web.${local.namespace}:3000" },
      # The NLB's nodes live in the public subnets: only they may send a PROXY header.
      { name = "QUANTLAB_EDGE_PROXY_CIDRS", value = join(" ", local.public_cidrs) },
    ]
    secrets = [{
      name      = local.secrets.edge_secret.env
      valueFrom = aws_secretsmanager_secret.app["edge_secret"].arn
    }]
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    mountPoints = [
      { sourceVolume = "caddy-data", containerPath = "/data", readOnly = false },
      { sourceVolume = "caddy-config", containerPath = "/config", readOnly = false },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.service["edge"].name
        awslogs-region        = var.region
        awslogs-stream-prefix = "edge"
      }
    }
  }])
}

resource "aws_ecs_service" "edge" {
  name                              = "edge"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.edge.arn
  desired_count                     = var.edge_count
  launch_type                       = "FARGATE"
  propagate_tags                    = "SERVICE"
  enable_execute_command            = false
  health_check_grace_period_seconds = 30

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = aws_subnet.app[*].id
    security_groups  = [aws_security_group.tier["edge"].id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.edge.arn
    container_name   = "edge"
    container_port   = 8080
  }

  depends_on = [aws_lb_listener.https]
}
