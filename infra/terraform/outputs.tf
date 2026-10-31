output "edge_dns_name" {
  description = "Point the domain's DNS (an alias or CNAME) at this NLB name."
  value       = aws_lb.edge.dns_name
}

output "ecr_repositories" {
  description = "Push api, web and edge images here, tagged var.image_tag."
  value       = { for k, r in aws_ecr_repository.image : k => r.repository_url }
}

output "secret_names" {
  description = "Secrets Manager names to set before the first deploy (required ones must have a value)."
  value       = { for k, s in aws_secretsmanager_secret.app : k => { name = s.name, required = local.secrets[k].required } }
}

output "database_endpoint" {
  description = "RDS endpoint for the application's database URL."
  value       = aws_db_instance.main.address
}

output "database_master_secret_arn" {
  description = "RDS-managed master credentials (for creating the application role)."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}

output "data_file_system_id" {
  description = "EFS file system holding QUANTLAB_DATA_DIR."
  value       = aws_efs_file_system.data.id
}

output "exports_bucket" {
  description = "S3 bucket for off-site exports."
  value       = aws_s3_bucket.exports.bucket
}

output "cluster_name" {
  description = "ECS cluster running the services."
  value       = aws_ecs_cluster.main.name
}

output "migrate_task_definition" {
  description = "Run this once per release, before updating the services."
  value       = aws_ecs_task_definition.migrate.arn
}

output "app_subnet_ids" {
  description = "Subnets for one-off tasks such as migrate."
  value       = aws_subnet.app[*].id
}

output "migrate_security_group_id" {
  description = "Security group for the one-off migrate task."
  value       = aws_security_group.tier["migrate"].id
}
