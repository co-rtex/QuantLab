# One security group per tier; each accepts traffic only from the tier in front of it:
# internet -> NLB -> edge -> web -> api -> {RDS, Redis, EFS}. Worker and reaper reach the
# data tier only. Rules are separate resources so the groups can reference each other.

locals {
  sg_names = ["nlb", "edge", "web", "api", "worker", "reaper", "migrate", "db", "redis", "efs"]
  # Tiers that run QuantLab Python code and need the data tier.
  python_tiers = ["api", "worker", "reaper", "migrate"]
}

resource "aws_security_group" "tier" {
  #checkov:skip=CKV2_AWS_5:Attached to ECS services, RDS, Redis, EFS and the NLB through for_each references
  for_each    = toset(local.sg_names)
  name        = "${local.name}-${each.key}"
  description = "QuantLab ${each.key} tier"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.name}-${each.key}" }
}

# --- internet -> NLB (TLS) ------------------------------------------------------------
resource "aws_vpc_security_group_ingress_rule" "nlb_https" {
  security_group_id = aws_security_group.tier["nlb"].id
  description       = "HTTPS from clients"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_vpc_security_group_egress_rule" "nlb_to_edge" {
  security_group_id            = aws_security_group.tier["nlb"].id
  description                  = "To the edge, with a PROXY v2 header"
  referenced_security_group_id = aws_security_group.tier["edge"].id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

# --- NLB -> edge -> web -> api ----------------------------------------------------------
locals {
  hops = {
    edge = { from = "nlb", port = 8080 }
    web  = { from = "edge", port = 3000 }
    api  = { from = "web", port = 8000 }
  }
}

resource "aws_vpc_security_group_ingress_rule" "hop" {
  for_each                     = local.hops
  security_group_id            = aws_security_group.tier[each.key].id
  description                  = "From the ${each.value.from} tier"
  referenced_security_group_id = aws_security_group.tier[each.value.from].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_egress_rule" "hop" {
  for_each                     = { for k, v in local.hops : v.from => { to = k, port = v.port } if v.from != "nlb" }
  security_group_id            = aws_security_group.tier[each.key].id
  description                  = "To the ${each.value.to} tier"
  referenced_security_group_id = aws_security_group.tier[each.value.to].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

# --- Python tiers -> data tier ----------------------------------------------------------
locals {
  data_ports = { db = 5432, redis = 6379, efs = 2049 }
  data_paths = merge([
    for src in local.python_tiers : {
      for dst, port in local.data_ports : "${src}-${dst}" => { src = src, dst = dst, port = port }
      # The migration task needs only the database.
      if src != "migrate" || dst == "db"
    }
  ]...)
}

resource "aws_vpc_security_group_egress_rule" "to_data" {
  for_each                     = local.data_paths
  security_group_id            = aws_security_group.tier[each.value.src].id
  description                  = "To ${each.value.dst}"
  referenced_security_group_id = aws_security_group.tier[each.value.dst].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

resource "aws_vpc_security_group_ingress_rule" "from_app" {
  for_each                     = local.data_paths
  security_group_id            = aws_security_group.tier[each.value.dst].id
  description                  = "From ${each.value.src}"
  referenced_security_group_id = aws_security_group.tier[each.value.src].id
  ip_protocol                  = "tcp"
  from_port                    = each.value.port
  to_port                      = each.value.port
}

# --- outbound HTTPS through NAT ---------------------------------------------------------
# Image pulls (ECR), Secrets Manager, CloudWatch Logs; for the worker also a licensed
# market-data vendor once one is configured (#11). VPC interface endpoints could replace
# this for the AWS services; they are left out to keep the module small.
resource "aws_vpc_security_group_egress_rule" "https_out" {
  for_each          = toset(["edge", "web", "api", "worker", "reaper", "migrate"])
  security_group_id = aws_security_group.tier[each.key].id
  description       = "HTTPS to AWS APIs and, for the worker, a market-data vendor"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
