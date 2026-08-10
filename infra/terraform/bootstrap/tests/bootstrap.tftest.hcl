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

  mock_resource "aws_iam_policy" {
    override_during = plan

    defaults = {
      arn = "arn:aws:iam::123456789012:policy/hindsight-github-deploy-observability"
    }
  }

}

mock_provider "cloudflare" {}

run "isolated_bootstrap" {
  command = plan

  variables {
    expected_aws_account_id = "123456789012"
    state_bucket_name       = "existing-state-bucket"
    cloudflare_zone_id      = "00000000000000000000000000000000"
    domain_name             = "hindsight.example.com"
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
    condition = toset(one([
      for statement in data.aws_iam_policy_document.github_deploy.statement : statement
      if statement.sid == "LifecycleArchiveMutationDenied"
      ]).resources) == toset([
      local.lifecycle_export_bucket_arn,
      "${local.lifecycle_export_bucket_arn}/*",
      local.lifecycle_recovery_bucket_arn,
      "${local.lifecycle_recovery_bucket_arn}/*",
    ])
    error_message = "Bootstrap must expose its OIDC trust anchor and deny deployment mutation of both lifecycle archives."
  }

  assert {
    condition = (
      aws_iam_role.github_observability_evidence.name == "hindsight-github-observability-evidence"
    )
    error_message = "Observability evidence must use an always-created stable GitHub OIDC role."
  }

  assert {
    condition     = aws_iam_role.github_evidence[0].name == "hindsight-github-evidence"
    error_message = "The evidence writer must use its dedicated GitHub OIDC role."
  }

  assert {
    condition     = aws_s3_bucket.learning_evidence[0].object_lock_enabled
    error_message = "The learning evidence bucket must enable Object Lock at creation."
  }

  assert {
    condition     = aws_s3_bucket_object_lock_configuration.learning_evidence[0].rule[0].default_retention[0].mode == "GOVERNANCE" && aws_s3_bucket_object_lock_configuration.learning_evidence[0].rule[0].default_retention[0].years == 7
    error_message = "Learning evidence must retain Governance-locked versions for seven years."
  }

  assert {
    condition     = aws_s3_bucket_versioning.learning_evidence[0].versioning_configuration[0].status == "Enabled"
    error_message = "The learning evidence bucket must keep versioning enabled."
  }

  assert {
    condition     = aws_kms_key.learning_corpus[0].enable_key_rotation && aws_kms_alias.learning_corpus[0].name == "alias/hindsight-demo-learning-corpus"
    error_message = "Protected corpus packages must use the stable rotating KMS key."
  }

  assert {
    condition     = aws_kms_key.learning_qualification_hmac[0].key_usage == "GENERATE_VERIFY_MAC" && aws_kms_key.learning_qualification_hmac[0].customer_master_key_spec == "HMAC_256"
    error_message = "Qualification identifiers must use a non-exportable HMAC-SHA256 KMS key."
  }

  assert {
    condition     = aws_kms_alias.learning_qualification_hmac[0].name == "alias/hindsight-demo-learning-qualification-hmac"
    error_message = "The qualification HMAC alias must remain stable."
  }

  assert {
    condition = length([
      for statement in data.aws_iam_policy_document.github_evidence[0].statement : statement
      if contains(statement.actions, "s3:DeleteObject") || contains(statement.actions, "s3:BypassGovernanceRetention")
    ]) == 0
    error_message = "The evidence writer must not delete objects or bypass retention."
  }


  assert {
    condition = length([
      for statement in data.aws_iam_policy_document.github_evidence[0].statement : statement
      if statement.sid == "QualificationOpaqueIdentifiers"
      ]) == 1 && toset(one([
        for statement in data.aws_iam_policy_document.github_evidence[0].statement : statement
        if statement.sid == "QualificationOpaqueIdentifiers"
    ]).actions) == toset(["kms:DescribeKey", "kms:GenerateMac", "kms:VerifyMac"])
    error_message = "The evidence role must have only the required qualification HMAC operations."
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

  assert {
    condition = (
      local.changefeed_function_arn == "arn:aws:lambda:us-east-1:123456789012:function:hindsight-demo-changefeed" &&
      length([
        for statement in data.aws_iam_policy_document.github_deploy.statement : statement
        if statement.sid == "ChangefeedConfigurationRead"
      ]) == 1 &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy.statement : statement
        if statement.sid == "ChangefeedConfigurationRead"
      ]).actions) == toset(["lambda:GetFunctionConfiguration"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy.statement : statement
        if statement.sid == "ChangefeedConfigurationRead"
      ]).resources) == toset([local.changefeed_function_arn])
    )
    error_message = "Changefeed configuration read access must contain one read action on only the stage changefeed function."
  }

  assert {
    condition = toset([
      for action in one([
        for statement in data.aws_iam_policy_document.github_deploy.statement : statement
        if statement.sid == "ApplicationLifecycle"
      ]).actions : action if startswith(action, "cognito-idp:Admin")
      ]) == toset([
      "cognito-idp:AdminAddUserToGroup",
      "cognito-idp:AdminCreateUser",
      "cognito-idp:AdminGetUser",
      "cognito-idp:AdminInitiateAuth",
      "cognito-idp:AdminListGroupsForUser",
      "cognito-idp:AdminRemoveUserFromGroup",
      "cognito-idp:AdminSetUserPassword",
    ])
    error_message = "The deployment role must expose only the admin identity operations used for provisioning and hosted token acquisition."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy.statement : statement
        if statement.sid == "CognitoUserPoolCreate"
      ]).actions) == toset(["cognito-idp:CreateUserPool"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy.statement : statement
        if statement.sid == "CognitoUserPoolCreate"
      ]).resources) == toset(["*"]) &&
      toset([
        for condition in one([
          for statement in data.aws_iam_policy_document.github_deploy.statement : statement
          if statement.sid == "CognitoUserPoolCreate"
        ]).condition : "${condition.test}:${condition.variable}:${join(",", condition.values)}"
        ]) == toset([
        "StringEquals:aws:RequestTag/Project:hindsight",
        "StringEquals:aws:RequestTag/Environment:demo",
        "StringEquals:aws:RequestTag/ManagedBy:terraform",
        "ForAllValues:StringEquals:aws:TagKeys:Environment,ManagedBy,Project",
      ]) &&
      !contains(one([
        for statement in data.aws_iam_policy_document.github_deploy.statement : statement
        if statement.sid == "ApplicationLifecycle"
      ]).actions, "cognito-idp:CreateUserPool")
    )
    error_message = "User-pool creation must require the exact stage-owned Terraform tags because Cognito does not support resource scoping before creation."
  }

  assert {
    condition = alltrue([
      for action in [
        "cognito-idp:CreateUserPoolClient",
        "cognito-idp:CreateUserPoolDomain",
        "cognito-idp:GetUserPoolMfaConfig",
        "wafv2:CreateWebACL",
        "wafv2:UpdateWebACL",
        ] : contains(one([
          for statement in data.aws_iam_policy_document.github_deploy.statement : statement
          if statement.sid == "ApplicationLifecycle"
      ]).actions, action)
    ])
    error_message = "The deployment role must be able to materialize and refresh the optional identity and edge-protection resources."
  }

  assert {
    condition = (
      aws_iam_policy.github_deploy_observability.name == local.github_deploy_observability_policy_name &&
      local.github_deploy_observability_policy_name == "hindsight-github-deploy-observability" &&
      aws_iam_policy.github_deploy_observability.arn == "arn:aws:iam::123456789012:policy/hindsight-github-deploy-observability" &&
      aws_iam_policy.github_deploy_observability.policy == data.aws_iam_policy_document.github_deploy_observability.json &&
      aws_iam_policy.github_deploy_observability.tags == tomap({
        Project     = "hindsight"
        Environment = "demo"
        ManagedBy   = "terraform-bootstrap"
      }) &&
      aws_iam_role_policy_attachment.github_deploy_observability.role == aws_iam_role.github_deploy.name &&
      aws_iam_role_policy_attachment.github_deploy_observability.policy_arn == aws_iam_policy.github_deploy_observability.arn &&
      toset(local.observability_topic_arns) == toset([
        "arn:aws:sns:us-east-1:123456789012:hindsight-demo-alerts",
        "arn:aws:sns:us-east-1:123456789012:hindsight-demo-budget-alerts",
      ]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityTopicLifecycle"
      ]).resources) == toset(local.observability_topic_arns) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityTopicLifecycle"
        ]).actions) == toset([
        "sns:CreateTopic",
        "sns:DeleteTopic",
        "sns:GetTopicAttributes",
        "sns:ListTagsForResource",
        "sns:SetTopicAttributes",
        "sns:TagResource",
        "sns:UntagResource",
      ])
    )
    error_message = "Observability access must use the attached size-bounded managed policy with stable identity and stage ownership tags."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilitySubscriptions"
      ]).resources) == toset(local.observability_subscription_arns) &&
      toset(local.observability_subscription_arns) == toset([
        "arn:aws:sns:us-east-1:123456789012:hindsight-demo-alerts",
        "arn:aws:sns:us-east-1:123456789012:hindsight-demo-alerts:*",
        "arn:aws:sns:us-east-1:123456789012:hindsight-demo-budget-alerts",
        "arn:aws:sns:us-east-1:123456789012:hindsight-demo-budget-alerts:*",
      ]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilitySubscriptions"
        ]).actions) == toset([
        "sns:GetSubscriptionAttributes",
        "sns:ListSubscriptionsByTopic",
        "sns:Subscribe",
        "sns:Unsubscribe",
      ]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityAlertExercise"
      ]).resources) == toset([local.observability_alert_topic_arn]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityAlertExercise"
      ]).actions) == toset(["sns:Publish"])
    )
    error_message = "Alert subscription permissions must stay on the two stage topics, and publish must stay on the operational topic."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityBudget"
        ]).actions) == toset([
        "budgets:ListTagsForResource",
        "budgets:ModifyBudget",
        "budgets:TagResource",
        "budgets:UntagResource",
        "budgets:ViewBudget",
      ]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityBudget"
      ]).resources) == toset([local.observability_budget_arn]) &&
      local.observability_budget_arn == "arn:aws:budgets::123456789012:budget/hindsight-demo-monthly-five-usd"
    )
    error_message = "Budget access, including Terraform tag reconciliation, must stay limited to the stage five-dollar budget."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilitySamplingRuleRead"
        ]).actions) == toset([
        "xray:GetSamplingRules",
        "xray:GetSamplingTargets",
      ]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilitySamplingRuleRead"
      ]).resources) == toset(["*"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilitySamplingRuleLifecycle"
      ]).resources) == toset([local.observability_sampling_rule_arn]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilitySamplingRuleLifecycle"
        ]).actions) == toset([
        "xray:CreateSamplingRule",
        "xray:DeleteSamplingRule",
        "xray:ListTagsForResource",
        "xray:TagResource",
        "xray:UntagResource",
        "xray:UpdateSamplingRule",
      ])
    )
    error_message = "X-Ray list reads must stay separate from the stage sampling-rule lifecycle."
  }

  assert {
    condition = (
      toset(flatten([
        for statement in data.aws_iam_policy_document.github_observability_evidence.statement : statement.actions
        ])) == toset([
        "sts:GetCallerIdentity",
        "logs:StartQuery",
        "logs:GetQueryResults",
        "logs:StopQuery",
        "xray:BatchGetTraces",
        "sns:Publish",
      ]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_observability_evidence.statement : statement
        if statement.sid == "BoundedLogQuery"
      ]).resources) == toset(local.observability_metric_log_group_arns) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_observability_evidence.statement : statement
        if statement.sid == "StageAlertPublish"
      ]).resources) == toset([local.observability_alert_topic_arn])
    )
    error_message = "The dedicated evidence role must contain only bounded reads and stage alert publication."
  }

  assert {
    condition = (
      length(local.observability_metric_log_group_arns) == 5 &&
      alltrue([
        for arn in local.observability_metric_log_group_arns : endswith(arn, ":*")
      ]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityMetricFilterRead"
      ]).resources) == toset(["*"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityMetricFilterLifecycle"
      ]).resources) == toset(local.observability_metric_log_group_arns) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityMetricFilterLifecycle"
      ]).actions) == toset(["logs:DeleteMetricFilter", "logs:PutMetricFilter"])
    )
    error_message = "Metric-filter writes must remain limited to the five bounded-profile log groups."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityAdotLayerRead"
      ]).actions) == toset(["lambda:GetLayerVersion"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_deploy_observability.statement : statement
        if statement.sid == "ObservabilityAdotLayerRead"
      ]).resources) == toset(local.observability_adot_layer_arns) &&
      toset(local.observability_adot_layer_arns) == toset(["arn:aws:lambda:us-east-1:901920570463:layer:aws-otel-python-amd64-*:*"])
    )
    error_message = "ADOT refresh must be read-only, Python-layer scoped, and attached to the existing deploy role."
  }
}

run "product_only_bootstrap" {
  command = plan

  variables {
    expected_aws_account_id        = "123456789012"
    state_bucket_name              = "target-state-bucket"
    application_state_key          = "hindsight/demo-candidate/terraform.tfstate"
    cloudflare_zone_id             = "00000000000000000000000000000000"
    domain_name                    = "candidate.hindsight.example.com"
    github_subjects                = ["repo:OCHOLA-EDDYPHIL/hindsight:environment:demo-candidate"]
    enable_learning_infrastructure = false
  }

  assert {
    condition     = aws_iam_role.github_deploy.name == "hindsight-github-deploy"
    error_message = "Product-only bootstrap must retain the deployment trust anchor."
  }

  assert {
    condition = (
      length(aws_s3_bucket.learning_evidence) == 0 &&
      length(aws_kms_key.learning_corpus) == 0 &&
      length(aws_kms_key.learning_qualification_hmac) == 0 &&
      length(aws_iam_role.github_evidence) == 0
    )
    error_message = "Product-only bootstrap must not create abandoned learning infrastructure."
  }

  assert {
    condition = length([
      for statement in data.aws_iam_policy_document.github_deploy.statement : statement
      if statement.sid == "EvidenceArchiveMutationDenied"
    ]) == 0
    error_message = "A product-only account must not reference a nonexistent learning archive."
  }

  assert {
    condition     = var.application_state_key == "hindsight/demo-candidate/terraform.tfstate"
    error_message = "Candidate bootstrap must scope the application state key independently."
  }

  assert {
    condition = toset(local.terraform_state_keys) == toset([
      "hindsight/demo-candidate/terraform.tfstate",
      "hindsight/edge/terraform.tfstate",
    ])
    error_message = "Target trust must scope application and stable-edge state independently."
  }
}
