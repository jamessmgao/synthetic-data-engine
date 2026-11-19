# Own VPC, so nothing here interacts with other projects in the account.
# Public subnets hold the runner (it needs the internet for Docker pulls and the
# inference API). Private subnets hold RDS and Redis and have no route out.
# There's no NAT gateway: the runner gets a public IP, and nothing listens on it.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = var.name }
}

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  tags              = { Name = "${var.name}-public-${local.azs[count.index]}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, 10 + count.index)
  tags              = { Name = "${var.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# --- security groups ---------------------------------------------------------

resource "aws_security_group" "runner" {
  name        = "${var.name}-runner"
  description = "Runner: no inbound (access is through SSM), all outbound"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# Runners reach each other (K3s across the runners, docs/KUBERNETES.md §10: the API on 6443,
# flannel VXLAN on 8472/udp, the kubelet on 10250). Only from this same group: still nothing
# inbound from outside.
resource "aws_vpc_security_group_ingress_rule" "runner_self" {
  security_group_id            = aws_security_group.runner.id
  referenced_security_group_id = aws_security_group.runner.id
  ip_protocol                  = "-1"
  description                  = "runner to runner (K3s)"
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "Postgres from the runner only"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.runner.id]
  }
}

resource "aws_security_group" "redis" {
  name        = "${var.name}-redis"
  description = "Redis from the runner only"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.runner.id]
  }
}
