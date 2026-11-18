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
  description = "Runs the app containers and up to SDE_DOCKER_SLOTS task containers (1 CPU / 2 GB each)."
  type        = string
  default     = "m6i.2xlarge"
}

variable "runner_disk_gb" {
  description = "Root volume: Docker images, task builds and the local work-dir cache."
  type        = number
  default     = 80
}

variable "docker_slots" {
  description = "Concurrent Harbor trials on the runner."
  type        = number
  default     = 4
}

variable "compose_version" {
  type    = string
  default = "2.32.4"
}

variable "buildx_version" {
  type    = string
  default = "0.19.3"
}
