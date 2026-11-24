# WARNING: do not apply this configuration against the stack that is already deployed.
#
# Every project-prefixed name here was changed to the `sde` prefix on 2026-11-24, in the repo only.
# The deployed stack (EC2 instances, RDS, ElastiCache, the S3 buckets, the SSM parameters) and
# its Terraform state still use the earlier prefix. Applying this configuration to that state would
# plan to destroy and recreate those resources, including the database and the artifacts bucket.
# Use it only for a new stack with a new state, or migrate the old one deliberately first.
# The differences are listed in README.md in this directory.
terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Created by `make aws-bootstrap` (deploy/aws_ops.py), not by Terraform itself.
  backend "s3" {
    bucket       = "sde-pipeline-tfstate-284460774261"
    key          = "sde-task-pipeline/terraform.tfstate"
    region       = "us-east-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
  # The account is shared with other projects: refuse to touch any other one.
  allowed_account_ids = [var.account_id]

  default_tags {
    tags = {
      project    = var.project
      managed_by = "terraform"
    }
  }
}
