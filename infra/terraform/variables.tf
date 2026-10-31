variable "region" {
  description = "AWS region."
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment name, used in resource names and tags."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.environment))
    error_message = "environment must be 2-21 lowercase letters, digits or hyphens."
  }
}

variable "vpc_cidr" {
  description = "VPC address range. Split into public (NLB), web, app and data subnets."
  type        = string
  default     = "10.40.0.0/16"
}

variable "az_count" {
  description = "Availability zones to spread subnets, the NLB, RDS and Redis over."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "az_count must be 2 or 3."
  }
}

variable "single_nat_gateway" {
  description = "One NAT gateway for all zones (cheaper, one zone of failure) instead of one per zone."
  type        = bool
  default     = false
}

variable "acm_certificate_arn" {
  description = "ARN of an existing ACM certificate for the public host name. The NLB terminates TLS with it. No default: an apply must name a real one."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/", var.acm_certificate_arn)) && !can(regex(":000000000000:", var.acm_certificate_arn))
    error_message = "acm_certificate_arn must be a real ACM certificate ARN (not the example's all-zero account)."
  }
}

variable "image_tag" {
  description = "Tag of the api, web and edge images in this deployment's ECR repositories (immutable tags). No default."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", var.image_tag)) && !can(regex("(?i)example|replace|unset|latest", var.image_tag))
    error_message = "image_tag must be a real, immutable release tag (not latest or an example value)."
  }
}

variable "redis_auth_token_version" {
  description = "Version of the generated Redis AUTH token. Increase it to rotate the token (README.md)."
  type        = number
  default     = 1
}

variable "api_count" {
  description = "API tasks."
  type        = number
  default     = 2
}

variable "api_workers" {
  description = "Uvicorn worker processes per API task (QUANTLAB_API_WORKERS)."
  type        = number
  default     = 2
}

variable "worker_count" {
  description = "Job worker tasks."
  type        = number
  default     = 2
}

variable "web_count" {
  description = "Web tier tasks."
  type        = number
  default     = 2
}

variable "edge_count" {
  description = "Edge (Caddy) tasks."
  type        = number
  default     = 2
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_allocated_storage_gb" {
  description = "Initial RDS storage (it grows to db_max_allocated_storage_gb)."
  type        = number
  default     = 50
}

variable "db_max_allocated_storage_gb" {
  description = "RDS storage autoscaling ceiling."
  type        = number
  default     = 500
}

variable "db_multi_az" {
  description = "Run RDS with a synchronous standby in a second zone."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Automated backup retention, which bounds point-in-time recovery."
  type        = number
  default     = 14

  validation {
    condition     = var.db_backup_retention_days >= 7 && var.db_backup_retention_days <= 35
    error_message = "Point-in-time recovery needs 7-35 days of automated backups."
  }
}

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.t4g.small"
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 365
}

variable "backup_retention_days" {
  description = "Days AWS Backup keeps daily recovery points of the data file system."
  type        = number
  default     = 35
}

variable "market_data_provider" {
  description = "Registered market-data vendor to enable (#11). Empty keeps the vendor path inert; set it only after the user chooses a vendor and licence."
  type        = string
  default     = ""
}

variable "market_data_license" {
  description = "The licence the user holds for market_data_provider (recorded in provenance)."
  type        = string
  default     = ""
}

variable "market_data_plan" {
  description = "The purchased plan for market_data_provider."
  type        = string
  default     = ""
}

variable "llm_enabled" {
  description = "Inject QUANTLAB_ANTHROPIC_API_KEY (its secret must then have a value)."
  type        = bool
  default     = false
}
