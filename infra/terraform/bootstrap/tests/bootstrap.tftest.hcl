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
}

run "isolated_bootstrap" {
  command = plan

  assert {
    condition     = aws_s3_bucket.state.bucket == "hindsight-terraform-123456789012-us-east-1"
    error_message = "Bootstrap state must use an account-scoped bucket."
  }

  assert {
    condition     = aws_iam_role.github_deploy.name == "hindsight-github-deploy"
    error_message = "The GitHub OIDC deployment role must remain stable."
  }
}
