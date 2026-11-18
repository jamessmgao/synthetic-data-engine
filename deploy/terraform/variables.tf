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
  default     = 4
}

variable "runner_disk_gb" {
  description = "Root volume: Docker images, task builds and the local work-dir cache."
  type        = number
  default     = 80
}

variable "docker_slots" {
  description = "Concurrent Harbor trials per runner. Task containers cap at 1 CPU / 2 GB but mostly idle waiting on the model, so both CPU (4x) and memory are oversubscribed; an OOM kill becomes an INVALID rollout and is retried. Lower it if `docker stats` shows memory pressure."
  type        = number
  default     = 8
}

variable "compose_version" {
  type    = string
  default = "2.32.4"
}

variable "buildx_version" {
  type    = string
  default = "0.19.3"
}
