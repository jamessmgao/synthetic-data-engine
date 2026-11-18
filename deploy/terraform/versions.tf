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
