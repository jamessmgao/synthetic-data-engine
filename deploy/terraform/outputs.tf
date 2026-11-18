output "runner_instance_ids" {
  description = "runner[0] is the control runner (API, worker-control, reconciler)."
  value       = aws_instance.runner[*].id
}

output "artifacts_bucket" {
  value = aws_s3_bucket.artifacts.bucket
}

output "db_endpoint" {
  value = aws_db_instance.main.address
}

output "redis_endpoint" {
  value = aws_elasticache_cluster.main.cache_nodes[0].address
}

output "region" {
  value = var.region
}

output "docker_slots" {
  value = var.docker_slots
}
