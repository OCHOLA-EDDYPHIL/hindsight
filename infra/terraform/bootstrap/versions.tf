terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.80, < 7.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = ">= 5.0, < 6.0"
    }
  }

  backend "s3" {}
}

provider "cloudflare" {}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = "hindsight"
      ManagedBy = "terraform-bootstrap"
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
      ManagedBy   = "terraform-bootstrap-recovery"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
