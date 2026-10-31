data "aws_availability_zones" "available" {
  #checkov:skip=CKV_AWS_394:Only the first az_count zones are used; a new zone does not change them
  state = "available"
}

locals {
  name = "quantlab-${var.environment}"
  azs  = slice(data.aws_availability_zones.available.names, 0, var.az_count)
  # /20 blocks: public (NLB nodes), web (only the web tier: the API trusts these CIDRs to
  # state client addresses, ADR-0008), app (api, worker, reaper), data (RDS, Redis, EFS).
  public_cidrs = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, i)]
  web_cidrs    = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, 4 + i)]
  app_cidrs    = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, 8 + i)]
  data_cidrs   = [for i in range(var.az_count) : cidrsubnet(var.vpc_cidr, 4, 12 + i)]
  nat_count    = var.single_nat_gateway ? 1 : var.az_count
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

# Nothing may use the default security group.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

resource "aws_subnet" "public" {
  count             = var.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.public_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.name}-public-${local.azs[count.index]}", Tier = "public" }
}

resource "aws_subnet" "web" {
  count             = var.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.web_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.name}-web-${local.azs[count.index]}", Tier = "web" }
}

resource "aws_subnet" "app" {
  count             = var.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.app_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.name}-app-${local.azs[count.index]}", Tier = "app" }
}

resource "aws_subnet" "data" {
  count             = var.az_count
  vpc_id            = aws_vpc.main.id
  cidr_block        = local.data_cidrs[count.index]
  availability_zone = local.azs[count.index]
  tags              = { Name = "${local.name}-data-${local.azs[count.index]}", Tier = "data" }
}

resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"
  tags   = { Name = "${local.name}-nat-${count.index}" }
}

resource "aws_nat_gateway" "main" {
  count         = local.nat_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id
  tags          = { Name = "${local.name}-nat-${count.index}" }
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = var.az_count
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Private tiers reach ECR, Secrets Manager, CloudWatch and (optionally) a market-data
# vendor through NAT. Nothing private has a public address.
resource "aws_route_table" "private" {
  count  = var.az_count
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[var.single_nat_gateway ? 0 : count.index].id
  }
  tags = { Name = "${local.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table_association" "web" {
  count          = var.az_count
  subnet_id      = aws_subnet.web[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table_association" "app" {
  count          = var.az_count
  subnet_id      = aws_subnet.app[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_route_table_association" "data" {
  count          = var.az_count
  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

resource "aws_flow_log" "vpc" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = "ALL"
  log_destination = aws_cloudwatch_log_group.flow.arn
  iam_role_arn    = aws_iam_role.flow_logs.arn
}

resource "aws_cloudwatch_log_group" "flow" {
  name              = "/quantlab/${var.environment}/vpc-flow"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn
}
