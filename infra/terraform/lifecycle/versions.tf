terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
  }

  backend "s3" {
    key          = "hindsight/demo/lifecycle/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "hindsight"
      Environment = var.stage
      ManagedBy   = "terraform-lifecycle"
    }
  }
}

provider "aws" {
  alias  = "cold_region_recovery"
  region = var.cold_region_recovery_region

  default_tags {
    tags = {
      Project     = "hindsight"
      Environment = var.stage
      ManagedBy   = "terraform-lifecycle-recovery"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
