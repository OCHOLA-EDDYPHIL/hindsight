mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:user/terraform-test"
      user_id    = "terraform-test"
    }
  }

  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  mock_data "aws_s3_bucket" {
    defaults = {
      id  = "existing-state-bucket"
      arn = "arn:aws:s3:::existing-state-bucket"
    }
  }

  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:us-east-1:123456789012:certificate/test"
      domain_validation_options = [
        {
          domain_name           = "hindsight.example.com"
          resource_record_name  = "_validation.hindsight.example.com"
          resource_record_type  = "CNAME"
          resource_record_value = "_validation.acm-validations.aws"
        }
      ]
    }
  }
}

mock_provider "cloudflare" {}

run "isolated_bootstrap" {
  command = plan

  variables {
    state_bucket_name  = "existing-state-bucket"
    cloudflare_zone_id = "00000000000000000000000000000000"
    domain_name        = "hindsight.example.com"
  }

  assert {
    condition     = data.aws_s3_bucket.state.id == "existing-state-bucket"
    error_message = "Bootstrap must reuse the configured state bucket."
  }

  assert {
    condition     = aws_iam_role.github_deploy.name == "hindsight-github-deploy"
    error_message = "The GitHub OIDC deployment role must remain stable."
  }

  assert {
    condition     = toset(var.github_subjects) == toset(["repo:OCHOLA-EDDYPHIL/hindsight:environment:demo"])
    error_message = "OIDC trust must remain scoped to the demo environment."
  }

  assert {
    condition     = local.lambda_version_refresh_actions == ["lambda:ListVersionsByFunction"]
    error_message = "Lambda version refresh must grant only ListVersionsByFunction."
  }

  assert {
    condition = length([
      for statement in data.aws_iam_policy_document.github_deploy.statement : statement
      if statement.sid == "LambdaVersionRefresh"
      ]) == 1 && toset(one([
        for statement in data.aws_iam_policy_document.github_deploy.statement : statement
        if statement.sid == "LambdaVersionRefresh"
    ]).actions) == toset(local.lambda_version_refresh_actions)
    error_message = "The deployment policy must bind the scoped action to one LambdaVersionRefresh statement."
  }

  assert {
    condition = toset(local.lambda_function_arns) == toset([
      "arn:aws:lambda:us-east-1:123456789012:function:hindsight-demo-api",
      "arn:aws:lambda:us-east-1:123456789012:function:hindsight-demo-worker",
      "arn:aws:lambda:us-east-1:123456789012:function:hindsight-demo-websocket",
      "arn:aws:lambda:us-east-1:123456789012:function:hindsight-demo-changefeed",
    ])
    error_message = "Lambda version refresh must remain scoped to the four application functions."
  }

  assert {
    condition = toset(one([
      for statement in data.aws_iam_policy_document.github_deploy.statement : statement
      if statement.sid == "LambdaVersionRefresh"
    ]).resources) == toset(local.lambda_function_arns)
    error_message = "The LambdaVersionRefresh statement must use only the four scoped function ARNs."
  }
}
