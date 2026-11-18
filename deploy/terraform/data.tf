# Managed state: Postgres (source of truth), single-node Redis (queue), S3 (artifacts).
# Sized and configured for a demo: smallest instances, single AZ, no final snapshot,
# deletable by `terraform destroy`.

resource "random_password" "db" {
  length  = 32
  special = false # it goes into a URL
}

resource "aws_db_subnet_group" "main" {
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "main" {
  identifier     = "${var.name}-db"
  engine         = "postgres"
  engine_version = "16.13"
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "sde"
  username = "sde"
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period = 1
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true
}

resource "aws_elasticache_subnet_group" "main" {
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_cluster" "main" {
  cluster_id           = "${var.name}-redis"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = "cache.t4g.micro"
  num_cache_nodes      = 1
  parameter_group_name = "default.redis7"
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]
}

resource "aws_s3_bucket" "artifacts" {
  bucket        = "${var.name}-artifacts-${var.account_id}"
  force_destroy = true # demo: `terraform destroy` removes the bucket and its contents
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Runtime config the runner reads at deploy time. The inference key is not here:
# `make aws-secrets` writes /sde/openrouter_api_key from .env, so it never enters
# Terraform state.
resource "aws_ssm_parameter" "database_url" {
  name  = "/sde/database_url"
  type  = "SecureString"
  value = "postgresql://sde:${random_password.db.result}@${aws_db_instance.main.address}:${aws_db_instance.main.port}/sde"
}

resource "aws_ssm_parameter" "redis_url" {
  name  = "/sde/redis_url"
  type  = "String"
  value = "redis://${aws_elasticache_cluster.main.cache_nodes[0].address}:${aws_elasticache_cluster.main.port}/0"
}

resource "aws_ssm_parameter" "s3_bucket" {
  name  = "/sde/s3_bucket"
  type  = "String"
  value = aws_s3_bucket.artifacts.bucket
}

# Per-runner pipeline settings, read by deploy/remote-deploy.sh into runtime.env.
resource "aws_ssm_parameter" "agent_slots" {
  name  = "/sde/agent_slots"
  type  = "String"
  value = tostring(var.agent_slots)
}

resource "aws_ssm_parameter" "agent_lease_ttl_s" {
  name  = "/sde/agent_lease_ttl_s"
  type  = "String"
  value = tostring(var.agent_lease_ttl_s)
}

resource "aws_ssm_parameter" "budget_usd" {
  name  = "/sde/budget_usd"
  type  = "String"
  value = tostring(var.budget_usd)
}
