variable "account_id" {
  description = "The only AWS account this config may touch."
  type        = string
  default     = "284460774261"
}

variable "region" {
  description = "Region (the organization's SCPs allow only us-east-1 and us-east-2)."
  type        = string
  default     = "us-east-2"
}

variable "project" {
  description = "Tag value on every resource, and the name prefix."
  type        = string
  default     = "sde-task-pipeline"
}

variable "name" {
  description = "Short prefix for resource names."
  type        = string
  default     = "sde-pipeline"
}

variable "runner_instance_type" {
  description = "The account is on the AWS Free plan, which allows only free-tier types; m7i-flex.large (2 vCPU / 8 GB) is the largest."
  type        = string
  default     = "m7i-flex.large"
}

variable "runner_count" {
  description = "Runners share the queue; each adds docker_slots of capacity. runner[0] also hosts the API and worker-control. The account's vCPU quota (16) allows 8."
  type        = number
  default     = 8 # the account's vCPU quota (16) at 2 vCPU each
}

variable "runner_disk_gb" {
  description = "Root volume: Docker images, task builds and the local work-dir cache."
  type        = number
  default     = 80
}

variable "docker_slots" {
  description = "Ceiling on Docker jobs per runner (2 vCPU / 8 GB). Jobs mostly wait on the model, so the real limit is memory: a runner takes no new Docker work below SDE_MIN_FREE_MEMORY_PCT (20%) free, i.e. it fills to about 80%."
  type        = number
  default     = 5 # 8 broke terminus-2 on 2 vCPU (tmux send-keys timeouts); raise with load-test data
}

variable "agent_slots" {
  description = "Ceiling on hour-long agent jobs (world, create / revise from a world) among a runner's docker_slots; the memory guard is the real limit."
  type        = number
  default     = 2
}

variable "agent_lease_ttl_s" {
  description = "Lease of an agent job; heartbeats renew it, so it only bounds how long a dead runner's job waits."
  type        = number
  default     = 1800
}

variable "budget_usd" {
  description = "Pipeline-wide spend cap (SDE_BUDGET_USD): no new paid work is claimed once recorded spend reaches it."
  type        = number
  default     = 40
}

variable "db_instance_class" {
  description = "RDS class. The account's AWS Free plan only allows db.t4g.micro (~80 max_connections); the runners' connection pools are sized to fit it (SDE_DB_POOL_MAX in remote-deploy.sh)."
  type        = string
  default     = "db.t4g.micro"
}

variable "compose_version" {
  type    = string
  default = "2.32.4"
}

variable "buildx_version" {
  type    = string
  default = "0.19.3"
}
