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
  description = "Runners share the queue; each adds docker_slots of capacity. runner[0] also hosts the API and worker-control. on_demand_count of them on-demand, the rest Spot."
  type        = number
  default     = 17 # 9 on-demand + 8 Spot (the user's call, 2026-11-18; Spot allowed since the Paid-plan upgrade lifted the Free plan's DenySpotMarket); new ones join K3s with `k8s_aws.py join`
}

variable "on_demand_count" {
  description = "Runners 0..on_demand_count-1 are on-demand (runner[0] is the K3s server, with api, control, the tunnels and the dashboard); the rest are Spot. 9 x 2 vCPU = the on-demand vCPU quota (18)."
  type        = number
  default     = 9
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
  description = "Pipeline-wide spend cap (SDE_BUDGET_USD): no new paid work is claimed once recorded spend reaches it. \"none\" = no cap."
  type        = string
  default     = "none" # the user's call, 2026-11-18 (was 40); an apply must not bring a cap back
}

variable "db_instance_class" {
  description = "RDS class. db.t4g.medium (4 GB): ~410 max_connections (micro had ~80), for 24 generation + 20 executor pods. Changing it modifies the DB in place (a few minutes' restart, apply_immediately)."
  type        = string
  default     = "db.t4g.medium" # the user's call, 2026-11-18 (Paid plan; was db.t4g.micro)
}

variable "compose_version" {
  type    = string
  default = "2.32.4"
}

variable "buildx_version" {
  type    = string
  default = "0.19.3"
}
