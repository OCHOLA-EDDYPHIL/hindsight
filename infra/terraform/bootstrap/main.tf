locals {
  state_bucket_arn                 = data.aws_s3_bucket.state.arn
  evidence_bucket_name             = "hindsight-${var.stage}-learning-evidence-${data.aws_caller_identity.current.account_id}"
  lifecycle_export_bucket_name     = "hindsight-${var.stage}-lifecycle-exports-${data.aws_caller_identity.current.account_id}"
  lifecycle_recovery_bucket_name   = "hindsight-${var.stage}-recovery-${data.aws_caller_identity.current.account_id}"
  lifecycle_export_bucket_arn      = "arn:${data.aws_partition.current.partition}:s3:::${local.lifecycle_export_bucket_name}"
  lifecycle_recovery_bucket_arn    = "arn:${data.aws_partition.current.partition}:s3:::${local.lifecycle_recovery_bucket_name}"
  lifecycle_export_retention_days  = 7
  lifecycle_export_expiration_days = 8
  lifecycle_replication_role_name  = "hindsight-lifecycle-replication-${var.stage}"
  lifecycle_replication_role_arn   = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${local.lifecycle_replication_role_name}"
  lifecycle_archive_deploy_denied_actions = [
    "s3:BypassGovernanceRetention",
    "s3:DeleteBucket",
    "s3:DeleteBucketPolicy",
    "s3:DeleteObject",
    "s3:DeleteObjectTagging",
    "s3:DeleteObjectVersion",
    "s3:PutBucketObjectLockConfiguration",
    "s3:PutBucketOwnershipControls",
    "s3:PutBucketPolicy",
    "s3:PutBucketPublicAccessBlock",
    "s3:PutBucketVersioning",
    "s3:PutEncryptionConfiguration",
    "s3:PutLifecycleConfiguration",
    "s3:PutObject",
    "s3:PutObjectLegalHold",
    "s3:PutObjectRetention",
    "s3:PutObjectTagging",
    "s3:PutReplicationConfiguration",
  ]
  oidc_arn                         = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : var.existing_github_oidc_provider_arn
  parameter_arn                    = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/hindsight/${var.stage}/*"
  lifecycle_database_parameter_arn = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.lifecycle_database_url_parameter_name}"
  evidence_parameter_arns = [
    "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/hindsight/${var.stage}/database-url",
    "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/hindsight/${var.stage}/gemini-api-keys",
  ]
  lifecycle_table_arns = [
    for table in ["realtime-tickets", "websocket-subscriptions", "websocket-connections"] :
    "arn:${data.aws_partition.current.partition}:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/hindsight-${var.stage}-${table}"
  ]
  lifecycle_table_index_arns = [
    for table_arn in local.lifecycle_table_arns : "${table_arn}/index/tenant-id-index"
  ]
  lifecycle_connection_table_arn  = "arn:${data.aws_partition.current.partition}:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/hindsight-${var.stage}-websocket-connections"
  lifecycle_cognito_user_pool_arn = "arn:${data.aws_partition.current.partition}:cognito-idp:${var.aws_region}:${data.aws_caller_identity.current.account_id}:userpool/*"
  lifecycle_websocket_arn         = "arn:${data.aws_partition.current.partition}:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/${var.stage}/DELETE/@connections/*"
  lambda_version_refresh_actions  = ["lambda:ListVersionsByFunction"]
  terraform_state_keys            = [var.application_state_key, var.edge_state_key]
  terraform_state_prefixes = flatten([
    for key in local.terraform_state_keys : [key, "${key}.*"]
  ])
  terraform_state_object_arns = flatten([
    for key in local.terraform_state_keys : [
      "${local.state_bucket_arn}/${key}",
      "${local.state_bucket_arn}/${key}.*",
    ]
  ])
  lambda_function_arns = [
    for component in ["api", "worker", "websocket", "changefeed"] :
    "arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:hindsight-${var.stage}-${component}"
  ]
  observability_alert_topic_arn  = "arn:${data.aws_partition.current.partition}:sns:${var.aws_region}:${data.aws_caller_identity.current.account_id}:hindsight-${var.stage}-alerts"
  observability_budget_topic_arn = "arn:${data.aws_partition.current.partition}:sns:us-east-1:${data.aws_caller_identity.current.account_id}:hindsight-${var.stage}-budget-alerts"
  observability_topic_arns = [
    local.observability_alert_topic_arn,
    local.observability_budget_topic_arn,
  ]
  observability_subscription_arns = flatten([
    for topic_arn in local.observability_topic_arns : [
      topic_arn,
      "${topic_arn}:*",
    ]
  ])
  observability_budget_arn        = "arn:${data.aws_partition.current.partition}:budgets::${data.aws_caller_identity.current.account_id}:budget/hindsight-${var.stage}-monthly-five-usd"
  observability_sampling_rule_arn = "arn:${data.aws_partition.current.partition}:xray:${var.aws_region}:${data.aws_caller_identity.current.account_id}:sampling-rule/hindsight-${var.stage}-bounded"
  observability_metric_log_group_arns = [
    for name in [
      "/aws/apigateway/hindsight-${var.stage}-http",
      "/aws/apigateway/hindsight-${var.stage}-websocket",
      "/aws/lambda/hindsight-${var.stage}-api",
      "/aws/lambda/hindsight-${var.stage}-worker",
      "/aws/lambda/hindsight-${var.stage}-changefeed",
    ] : "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:${name}"
  ]
  observability_adot_layer_arns = [
    "arn:aws:lambda:${var.aws_region}:901920570463:layer:aws-otel-python-*:*",
  ]
}

check "expected_aws_account" {
  assert {
    condition     = data.aws_caller_identity.current.account_id == var.expected_aws_account_id
    error_message = "Authenticated AWS account does not match expected_aws_account_id."
  }
}

check "cold_region_recovery_profile_regions" {
  assert {
    condition     = !var.enable_cold_region_recovery_profile || var.cold_region_recovery_region != var.aws_region
    error_message = "cold_region_recovery_region must differ from aws_region when the recovery profile is enabled."
  }
}

check "lifecycle_database_parameter_stage" {
  assert {
    condition     = var.lifecycle_database_url_parameter_name == "/hindsight/${var.stage}/lifecycle-database-url"
    error_message = "lifecycle_database_url_parameter_name must match the selected bootstrap stage."
  }
}

data "aws_s3_bucket" "state" {
  bucket = var.state_bucket_name
}

resource "aws_s3_bucket" "tenant_lifecycle_exports" {
  bucket              = local.lifecycle_export_bucket_name
  object_lock_enabled = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tenant_lifecycle_exports" {
  bucket = aws_s3_bucket.tenant_lifecycle_exports.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tenant_lifecycle_exports" {
  bucket = aws_s3_bucket.tenant_lifecycle_exports.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tenant_lifecycle_exports" {
  bucket                  = aws_s3_bucket.tenant_lifecycle_exports.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tenant_lifecycle_exports" {
  bucket = aws_s3_bucket.tenant_lifecycle_exports.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "tenant_lifecycle_exports" {
  bucket = aws_s3_bucket.tenant_lifecycle_exports.id

  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = local.lifecycle_export_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.tenant_lifecycle_exports]
}

resource "aws_s3_bucket_lifecycle_configuration" "tenant_lifecycle_exports" {
  bucket = aws_s3_bucket.tenant_lifecycle_exports.id

  rule {
    id     = "expire-tenant-exports-after-retention"
    status = "Enabled"

    filter {
      prefix = "tenant-exports/"
    }

    expiration {
      days = local.lifecycle_export_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = local.lifecycle_export_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [
    aws_s3_bucket_object_lock_configuration.tenant_lifecycle_exports,
    aws_s3_bucket_versioning.tenant_lifecycle_exports,
  ]
}

resource "aws_s3_bucket" "tenant_lifecycle_recovery" {
  count    = var.enable_cold_region_recovery_profile ? 1 : 0
  provider = aws.cold_region_recovery

  bucket              = local.lifecycle_recovery_bucket_name
  object_lock_enabled = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tenant_lifecycle_recovery" {
  count    = var.enable_cold_region_recovery_profile ? 1 : 0
  provider = aws.cold_region_recovery

  bucket = aws_s3_bucket.tenant_lifecycle_recovery[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tenant_lifecycle_recovery" {
  count    = var.enable_cold_region_recovery_profile ? 1 : 0
  provider = aws.cold_region_recovery

  bucket = aws_s3_bucket.tenant_lifecycle_recovery[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tenant_lifecycle_recovery" {
  count    = var.enable_cold_region_recovery_profile ? 1 : 0
  provider = aws.cold_region_recovery

  bucket                  = aws_s3_bucket.tenant_lifecycle_recovery[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "tenant_lifecycle_recovery" {
  count    = var.enable_cold_region_recovery_profile ? 1 : 0
  provider = aws.cold_region_recovery

  bucket = aws_s3_bucket.tenant_lifecycle_recovery[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "tenant_lifecycle_recovery" {
  count    = var.enable_cold_region_recovery_profile ? 1 : 0
  provider = aws.cold_region_recovery

  bucket = aws_s3_bucket.tenant_lifecycle_recovery[0].id

  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = local.lifecycle_export_retention_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.tenant_lifecycle_recovery[0]]
}

resource "aws_s3_bucket_lifecycle_configuration" "tenant_lifecycle_recovery" {
  count    = var.enable_cold_region_recovery_profile ? 1 : 0
  provider = aws.cold_region_recovery

  bucket = aws_s3_bucket.tenant_lifecycle_recovery[0].id

  rule {
    id     = "expire-replicated-tenant-exports-after-retention"
    status = "Enabled"

    filter {
      prefix = "tenant-exports/"
    }

    expiration {
      days = local.lifecycle_export_expiration_days
    }

    noncurrent_version_expiration {
      noncurrent_days = local.lifecycle_export_expiration_days
    }
  }

  depends_on = [
    aws_s3_bucket_object_lock_configuration.tenant_lifecycle_recovery[0],
    aws_s3_bucket_versioning.tenant_lifecycle_recovery[0],
  ]
}

resource "aws_s3_bucket" "learning_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  bucket              = local.evidence_bucket_name
  object_lock_enabled = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_key" "learning_corpus" {
  count = var.enable_learning_infrastructure ? 1 : 0

  description             = "Hindsight protected learning corpus packages"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_key" "learning_qualification_hmac" {
  count = var.enable_learning_infrastructure ? 1 : 0

  description              = "Hindsight learning qualification opaque identifiers"
  key_usage                = "GENERATE_VERIFY_MAC"
  customer_master_key_spec = "HMAC_256"
  deletion_window_in_days  = 30
  enable_key_rotation      = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "learning_corpus" {
  count = var.enable_learning_infrastructure ? 1 : 0

  name          = "alias/hindsight-${var.stage}-learning-corpus"
  target_key_id = aws_kms_key.learning_corpus[0].key_id
}

resource "aws_kms_alias" "learning_qualification_hmac" {
  count = var.enable_learning_infrastructure ? 1 : 0

  name          = "alias/hindsight-${var.stage}-learning-qualification-hmac"
  target_key_id = aws_kms_key.learning_qualification_hmac[0].key_id
}

resource "aws_s3_bucket_versioning" "learning_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  bucket = aws_s3_bucket.learning_evidence[0].id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "learning_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  bucket = aws_s3_bucket.learning_evidence[0].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "learning_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  bucket                  = aws_s3_bucket.learning_evidence[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "learning_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  bucket = aws_s3_bucket.learning_evidence[0].id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "learning_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  bucket = aws_s3_bucket.learning_evidence[0].id
  rule {
    default_retention {
      mode  = "GOVERNANCE"
      years = 7
    }
  }
  depends_on = [aws_s3_bucket_versioning.learning_evidence[0]]
}

resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_github_oidc_provider ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_acm_certificate" "demo" {
  domain_name       = var.domain_name
  validation_method = "DNS"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_dns_record" "acm_validation" {
  for_each = {
    (var.domain_name) = one(aws_acm_certificate.demo.domain_validation_options)
  }

  zone_id = var.cloudflare_zone_id
  name    = each.value.resource_record_name
  content = each.value.resource_record_value
  type    = each.value.resource_record_type
  ttl     = 1
  proxied = false
  comment = "ACM validation for Hindsight"
}

resource "aws_acm_certificate_validation" "demo" {
  certificate_arn         = aws_acm_certificate.demo.arn
  validation_record_fqdns = [for record in cloudflare_dns_record.acm_validation : record.name]
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = var.github_subjects
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  name                 = "hindsight-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role" "github_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  name                 = "hindsight-github-evidence"
  assume_role_policy   = data.aws_iam_policy_document.github_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role" "github_lifecycle" {
  name                 = "hindsight-github-lifecycle"
  assume_role_policy   = data.aws_iam_policy_document.github_assume.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "lifecycle_export_replication_assume" {
  count = var.enable_cold_region_recovery_profile ? 1 : 0

  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["s3.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lifecycle_export_replication" {
  count = var.enable_cold_region_recovery_profile ? 1 : 0

  name               = local.lifecycle_replication_role_name
  assume_role_policy = data.aws_iam_policy_document.lifecycle_export_replication_assume[0].json
}

data "aws_iam_policy_document" "lifecycle_export_replication" {
  count = var.enable_cold_region_recovery_profile ? 1 : 0

  statement {
    sid = "ReadSourceReplicationConfiguration"
    actions = [
      "s3:GetReplicationConfiguration",
      "s3:ListBucket",
    ]
    resources = [local.lifecycle_export_bucket_arn]
  }

  statement {
    sid = "ReadLockedSourceVersions"
    actions = [
      "s3:GetObjectLegalHold",
      "s3:GetObjectRetention",
      "s3:GetObjectVersionAcl",
      "s3:GetObjectVersionForReplication",
      "s3:GetObjectVersionTagging",
    ]
    resources = ["${local.lifecycle_export_bucket_arn}/tenant-exports/*"]
  }

  statement {
    sid = "ReplicateLockedVersions"
    actions = [
      "s3:ReplicateObject",
      "s3:ReplicateTags",
    ]
    resources = ["${local.lifecycle_recovery_bucket_arn}/tenant-exports/*"]
  }
}

resource "aws_iam_role_policy" "lifecycle_export_replication" {
  count = var.enable_cold_region_recovery_profile ? 1 : 0

  role   = aws_iam_role.lifecycle_export_replication[0].id
  policy = data.aws_iam_policy_document.lifecycle_export_replication[0].json
}

resource "aws_s3_bucket_replication_configuration" "tenant_lifecycle_exports" {
  count = var.enable_cold_region_recovery_profile ? 1 : 0

  bucket = aws_s3_bucket.tenant_lifecycle_exports.id
  role   = aws_iam_role.lifecycle_export_replication[0].arn

  rule {
    id     = "replicate-locked-tenant-exports"
    status = "Enabled"

    filter {
      prefix = "tenant-exports/"
    }

    delete_marker_replication {
      status = "Disabled"
    }

    destination {
      bucket        = local.lifecycle_recovery_bucket_arn
      storage_class = "STANDARD"
    }
  }

  depends_on = [
    aws_iam_role_policy.lifecycle_export_replication[0],
    aws_s3_bucket_object_lock_configuration.tenant_lifecycle_exports,
    aws_s3_bucket_object_lock_configuration.tenant_lifecycle_recovery[0],
    aws_s3_bucket_policy.tenant_lifecycle_exports,
    aws_s3_bucket_policy.tenant_lifecycle_recovery[0],
    aws_s3_bucket_versioning.tenant_lifecycle_exports,
    aws_s3_bucket_versioning.tenant_lifecycle_recovery[0],
  ]
}

data "aws_iam_policy_document" "github_deploy" {
  statement {
    sid       = "TerraformStateBucketMetadata"
    actions   = ["s3:GetBucketLocation", "s3:GetBucketVersioning"]
    resources = [local.state_bucket_arn]
  }

  statement {
    sid       = "TerraformStateList"
    actions   = ["s3:ListBucket"]
    resources = [local.state_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = local.terraform_state_prefixes
    }
  }

  statement {
    sid = "TerraformStateObject"
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:PutObject"
    ]
    resources = local.terraform_state_object_arns
  }

  statement {
    sid       = "CertificateReadiness"
    actions   = ["acm:DescribeCertificate"]
    resources = [aws_acm_certificate.demo.arn]
  }

  statement {
    sid       = "ParameterReadiness"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [local.parameter_arn]
  }

  statement {
    sid       = "LambdaVersionRefresh"
    actions   = local.lambda_version_refresh_actions
    resources = local.lambda_function_arns
  }

  dynamic "statement" {
    for_each = var.enable_learning_infrastructure ? [true] : []
    content {
      sid    = "EvidenceArchiveMutationDenied"
      effect = "Deny"
      actions = [
        "s3:BypassGovernanceRetention",
        "s3:DeleteBucket",
        "s3:DeleteBucketPolicy",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:PutBucketObjectLockConfiguration",
        "s3:PutBucketPolicy",
        "s3:PutBucketVersioning",
        "s3:PutObject",
        "s3:PutObjectRetention",
      ]
      resources = [
        aws_s3_bucket.learning_evidence[0].arn,
        "${aws_s3_bucket.learning_evidence[0].arn}/*",
      ]
    }
  }

  statement {
    sid     = "LifecycleArchiveMutationDenied"
    effect  = "Deny"
    actions = local.lifecycle_archive_deploy_denied_actions
    resources = concat(
      [
        local.lifecycle_export_bucket_arn,
        "${local.lifecycle_export_bucket_arn}/*",
      ],
      var.enable_cold_region_recovery_profile ? [
        local.lifecycle_recovery_bucket_arn,
        "${local.lifecycle_recovery_bucket_arn}/*",
      ] : [],
    )
  }

  statement {
    sid = "ApplicationLifecycle"
    actions = [
      "apigateway:DELETE",
      "apigateway:GET",
      "apigateway:PATCH",
      "apigateway:POST",
      "apigateway:PUT",
      "apigateway:TagResource",
      "apigateway:UntagResource",
      "cloudfront:CreateDistribution",
      "cloudfront:CreateInvalidation",
      "cloudfront:DeleteDistribution",
      "cloudfront:GetCachePolicy",
      "cloudfront:GetDistribution",
      "cloudfront:GetDistributionConfig",
      "cloudfront:GetInvalidation",
      "cloudfront:GetOriginAccessControl",
      "cloudfront:GetOriginAccessControlConfig",
      "cloudfront:GetOriginRequestPolicy",
      "cloudfront:ListCachePolicies",
      "cloudfront:ListDistributions",
      "cloudfront:ListOriginAccessControls",
      "cloudfront:ListOriginRequestPolicies",
      "cloudfront:ListTagsForResource",
      "cloudfront:TagResource",
      "cloudfront:UntagResource",
      "cloudfront:UpdateDistribution",
      "cloudfront:CreateOriginAccessControl",
      "cloudfront:DeleteOriginAccessControl",
      "cloudfront:UpdateOriginAccessControl",
      "cloudwatch:DeleteAlarms",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:ListTagsForResource",
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:TagResource",
      "cloudwatch:UntagResource",
      "cognito-idp:AdminAddUserToGroup",
      "cognito-idp:AdminCreateUser",
      "cognito-idp:AdminGetUser",
      "cognito-idp:AdminInitiateAuth",
      "cognito-idp:AdminListGroupsForUser",
      "cognito-idp:AdminRemoveUserFromGroup",
      "cognito-idp:AdminSetUserPassword",
      "cognito-idp:CreateGroup",
      "cognito-idp:CreateUserPool",
      "cognito-idp:CreateUserPoolClient",
      "cognito-idp:CreateUserPoolDomain",
      "cognito-idp:DeleteGroup",
      "cognito-idp:DeleteUserPool",
      "cognito-idp:DeleteUserPoolClient",
      "cognito-idp:DeleteUserPoolDomain",
      "cognito-idp:DescribeUserPool",
      "cognito-idp:DescribeUserPoolClient",
      "cognito-idp:DescribeUserPoolDomain",
      "cognito-idp:GetGroup",
      "cognito-idp:ListTagsForResource",
      "cognito-idp:ListUserPoolClients",
      "cognito-idp:ListUserPools",
      "cognito-idp:ListUsers",
      "cognito-idp:ListUsersInGroup",
      "cognito-idp:TagResource",
      "cognito-idp:UntagResource",
      "cognito-idp:UpdateGroup",
      "cognito-idp:UpdateUserPool",
      "cognito-idp:UpdateUserPoolClient",
      "dynamodb:CreateTable",
      "dynamodb:BatchGetItem",
      "dynamodb:DeleteItem",
      "dynamodb:DeleteTable",
      "dynamodb:DescribeContinuousBackups",
      "dynamodb:DescribeTable",
      "dynamodb:DescribeTimeToLive",
      "dynamodb:GetItem",
      "dynamodb:ListTagsOfResource",
      "dynamodb:PutItem",
      "dynamodb:TagResource",
      "dynamodb:UntagResource",
      "dynamodb:UpdateContinuousBackups",
      "dynamodb:UpdateItem",
      "dynamodb:UpdateTable",
      "dynamodb:UpdateTimeToLive",
      "events:DeleteRule",
      "events:DescribeRule",
      "events:DisableRule",
      "events:EnableRule",
      "events:ListTagsForResource",
      "events:ListTargetsByRule",
      "events:PutRule",
      "events:PutTargets",
      "events:RemoveTargets",
      "events:TagResource",
      "events:UntagResource",
      "lambda:AddPermission",
      "lambda:CreateEventSourceMapping",
      "lambda:CreateFunction",
      "lambda:DeleteEventSourceMapping",
      "lambda:DeleteFunction",
      "lambda:DeleteFunctionConcurrency",
      "lambda:GetEventSourceMapping",
      "lambda:GetFunction",
      "lambda:GetFunctionCodeSigningConfig",
      "lambda:GetPolicy",
      "lambda:ListTags",
      "lambda:PutFunctionConcurrency",
      "lambda:RemovePermission",
      "lambda:TagResource",
      "lambda:UntagResource",
      "lambda:UpdateEventSourceMapping",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "logs:CreateLogGroup",
      "logs:CreateLogDelivery",
      "logs:DeleteLogGroup",
      "logs:DeleteLogDelivery",
      "logs:DescribeLogGroups",
      "logs:DescribeResourcePolicies",
      "logs:GetLogDelivery",
      "logs:ListTagsForResource",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:PutRetentionPolicy",
      "logs:TagResource",
      "logs:UntagResource",
      "logs:UpdateLogDelivery",
      "s3:CreateBucket",
      "s3:DeleteBucket",
      "s3:DeleteBucketPolicy",
      "s3:DeleteObject",
      "s3:DeleteObjectTagging",
      "s3:DeleteObjectVersion",
      "s3:GetAccelerateConfiguration",
      "s3:GetBucketAcl",
      "s3:GetBucketCORS",
      "s3:GetBucketLocation",
      "s3:GetBucketLogging",
      "s3:GetBucketNotification",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketOwnershipControls",
      "s3:GetBucketPolicy",
      "s3:GetBucketPolicyStatus",
      "s3:GetBucketPublicAccessBlock",
      "s3:GetBucketRequestPayment",
      "s3:GetBucketTagging",
      "s3:GetBucketVersioning",
      "s3:GetBucketWebsite",
      "s3:GetEncryptionConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:GetObject",
      "s3:GetObjectTagging",
      "s3:GetReplicationConfiguration",
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:PutBucketPolicy",
      "s3:PutBucketPublicAccessBlock",
      "s3:PutBucketTagging",
      "s3:PutBucketVersioning",
      "s3:PutEncryptionConfiguration",
      "s3:PutObject",
      "s3:PutObjectTagging",
      "sqs:CreateQueue",
      "sqs:DeleteQueue",
      "sqs:GetQueueAttributes",
      "sqs:GetQueueUrl",
      "sqs:ListQueueTags",
      "sqs:ListQueues",
      "sqs:SetQueueAttributes",
      "sqs:TagQueue",
      "sqs:UntagQueue",
      "sts:GetCallerIdentity",
      "wafv2:CreateWebACL",
      "wafv2:DeleteWebACL",
      "wafv2:DescribeManagedRuleGroup",
      "wafv2:GetWebACL",
      "wafv2:ListAvailableManagedRuleGroups",
      "wafv2:ListTagsForResource",
      "wafv2:ListWebACLs",
      "wafv2:TagResource",
      "wafv2:UntagResource",
      "wafv2:UpdateWebACL"
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ControlledIncidentTelemetryWrite"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["Hindsight/ControlledIncidentTelemetry"]
    }
  }

  statement {
    sid       = "ControlledIncidentTelemetryRead"
    actions   = ["cloudwatch:GetMetricStatistics"]
    resources = ["*"]
  }

  statement {
    sid = "ApplicationIam"
    actions = [
      "iam:AttachRolePolicy",
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:DeleteRolePolicy",
      "iam:DetachRolePolicy",
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRolePolicies",
      "iam:PassRole",
      "iam:PutRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:UpdateAssumeRolePolicy"
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/hindsight-${var.stage}-*"
    ]
  }
}

resource "aws_iam_role_policy" "github_deploy" {
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy.json
}

data "aws_iam_policy_document" "github_deploy_observability" {
  statement {
    sid = "ObservabilityTopicLifecycle"
    actions = [
      "sns:CreateTopic",
      "sns:DeleteTopic",
      "sns:GetTopicAttributes",
      "sns:ListTagsForResource",
      "sns:SetTopicAttributes",
      "sns:TagResource",
      "sns:UntagResource",
    ]
    resources = local.observability_topic_arns
  }

  statement {
    sid = "ObservabilitySubscriptions"
    actions = [
      "sns:GetSubscriptionAttributes",
      "sns:ListSubscriptionsByTopic",
      "sns:Subscribe",
      "sns:Unsubscribe",
    ]
    resources = local.observability_subscription_arns
  }

  statement {
    sid       = "ObservabilityAlertExercise"
    actions   = ["sns:Publish"]
    resources = [local.observability_alert_topic_arn]
  }

  statement {
    sid       = "ObservabilityBudget"
    actions   = ["budgets:ModifyBudget", "budgets:ViewBudget"]
    resources = [local.observability_budget_arn]
  }

  statement {
    sid = "ObservabilitySamplingRuleRead"
    actions = [
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }

  statement {
    sid = "ObservabilitySamplingRuleLifecycle"
    actions = [
      "xray:CreateSamplingRule",
      "xray:DeleteSamplingRule",
      "xray:ListTagsForResource",
      "xray:TagResource",
      "xray:UntagResource",
      "xray:UpdateSamplingRule",
    ]
    resources = [local.observability_sampling_rule_arn]
  }

  statement {
    sid       = "ObservabilityMetricFilterRead"
    actions   = ["logs:DescribeMetricFilters"]
    resources = ["*"]
  }

  statement {
    sid = "ObservabilityMetricFilterLifecycle"
    actions = [
      "logs:DeleteMetricFilter",
      "logs:PutMetricFilter",
    ]
    resources = local.observability_metric_log_group_arns
  }

  statement {
    sid       = "ObservabilityAdotLayerRead"
    actions   = ["lambda:GetLayerVersion"]
    resources = local.observability_adot_layer_arns
  }
}

resource "aws_iam_role_policy" "github_deploy_observability" {
  role   = aws_iam_role.github_deploy.id
  policy = data.aws_iam_policy_document.github_deploy_observability.json
}

data "aws_iam_policy_document" "github_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  statement {
    sid       = "EvidenceSettings"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = local.evidence_parameter_arns
  }

  statement {
    sid = "ProtectedCorpusEncryption"
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = [aws_kms_key.learning_corpus[0].arn]
  }

  statement {
    sid = "QualificationOpaqueIdentifiers"
    actions = [
      "kms:DescribeKey",
      "kms:GenerateMac",
      "kms:VerifyMac",
    ]
    resources = [aws_kms_key.learning_qualification_hmac[0].arn]
  }

  statement {
    sid = "EvidenceBucketMetadata"
    actions = [
      "s3:GetBucketLocation",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
    ]
    resources = [aws_s3_bucket.learning_evidence[0].arn]
  }

  statement {
    sid = "EvidenceBucketList"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
    ]
    resources = [aws_s3_bucket.learning_evidence[0].arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["learning", "learning/*"]
    }
  }

  statement {
    sid = "AppendAndVerifyEvidence"
    actions = [
      "s3:GetObject",
      "s3:GetObjectAttributes",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.learning_evidence[0].arn}/learning/*"]
  }
}

resource "aws_iam_role_policy" "github_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  role   = aws_iam_role.github_evidence[0].id
  policy = data.aws_iam_policy_document.github_evidence[0].json
}

data "aws_iam_policy_document" "github_lifecycle" {
  statement {
    sid       = "LifecycleDatabaseSettings"
    actions   = ["ssm:GetParameter"]
    resources = [local.lifecycle_database_parameter_arn]
  }

  statement {
    sid = "LifecycleExportBucketMetadata"
    actions = [
      "s3:GetBucketLocation",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
    ]
    resources = [local.lifecycle_export_bucket_arn]
  }

  statement {
    sid = "LifecycleExportBucketList"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
    ]
    resources = [local.lifecycle_export_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["tenant-exports", "tenant-exports/*"]
    }
  }

  statement {
    sid       = "LifecycleExportMultipartList"
    actions   = ["s3:ListBucketMultipartUploads"]
    resources = [local.lifecycle_export_bucket_arn]
  }

  statement {
    sid = "WriteAndVerifyLifecycleExports"
    actions = [
      "s3:AbortMultipartUpload",
      "s3:GetObject",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:ListMultipartUploadParts",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = ["${local.lifecycle_export_bucket_arn}/tenant-exports/*"]
  }

  statement {
    sid       = "TenantStateGet"
    actions   = ["dynamodb:GetItem"]
    resources = local.lifecycle_table_arns
  }

  statement {
    sid       = "TenantStateQuery"
    actions   = ["dynamodb:Query"]
    resources = concat(local.lifecycle_table_arns, local.lifecycle_table_index_arns)
  }

  statement {
    sid       = "TenantStateScan"
    actions   = ["dynamodb:Scan"]
    resources = local.lifecycle_table_arns
  }

  statement {
    sid       = "TenantStateDelete"
    actions   = ["dynamodb:DeleteItem"]
    resources = local.lifecycle_table_arns
  }

  statement {
    sid       = "TenantRealtimeFence"
    actions   = ["dynamodb:PutItem"]
    resources = [local.lifecycle_connection_table_arn]
  }

  statement {
    sid = "TenantIdentityCleanup"
    actions = [
      "cognito-idp:AdminDeleteUser",
      "cognito-idp:AdminGetUser",
    ]
    resources = [local.lifecycle_cognito_user_pool_arn]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = ["hindsight"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Environment"
      values   = [var.stage]
    }
  }

  statement {
    sid       = "TenantWebSocketDisconnect"
    actions   = ["execute-api:ManageConnections"]
    resources = [local.lifecycle_websocket_arn]
  }
}

resource "aws_iam_role_policy" "github_lifecycle" {
  role   = aws_iam_role.github_lifecycle.id
  policy = data.aws_iam_policy_document.github_lifecycle.json
}

data "aws_iam_policy_document" "learning_evidence_bucket" {
  count = var.enable_learning_infrastructure ? 1 : 0

  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"
    actions = [
      "s3:BypassGovernanceRetention",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:GetBucketLocation",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
      "s3:GetObject",
      "s3:GetObjectAttributes",
      "s3:GetObjectRetention",
      "s3:GetObjectVersion",
      "s3:ListBucket",
      "s3:ListBucketVersions",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = [
      aws_s3_bucket.learning_evidence[0].arn,
      "${aws_s3_bucket.learning_evidence[0].arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyDeploymentMutation"
    effect = "Deny"
    actions = [
      "s3:BypassGovernanceRetention",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObject",
      "s3:PutObjectRetention",
    ]
    resources = ["${aws_s3_bucket.learning_evidence[0].arn}/*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.github_deploy.arn]
    }
  }
}

data "aws_iam_policy_document" "tenant_lifecycle_exports" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      local.lifecycle_export_bucket_arn,
      "${local.lifecycle_export_bucket_arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid     = "DenyDeploymentMutation"
    effect  = "Deny"
    actions = local.lifecycle_archive_deploy_denied_actions
    resources = [
      local.lifecycle_export_bucket_arn,
      "${local.lifecycle_export_bucket_arn}/*",
    ]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.github_deploy.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "tenant_lifecycle_exports" {
  bucket     = aws_s3_bucket.tenant_lifecycle_exports.id
  policy     = data.aws_iam_policy_document.tenant_lifecycle_exports.json
  depends_on = [aws_s3_bucket_public_access_block.tenant_lifecycle_exports]
}

data "aws_iam_policy_document" "tenant_lifecycle_recovery" {
  count = var.enable_cold_region_recovery_profile ? 1 : 0

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    resources = [
      local.lifecycle_recovery_bucket_arn,
      "${local.lifecycle_recovery_bucket_arn}/*",
    ]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid     = "DenyDeploymentMutation"
    effect  = "Deny"
    actions = local.lifecycle_archive_deploy_denied_actions
    resources = [
      local.lifecycle_recovery_bucket_arn,
      "${local.lifecycle_recovery_bucket_arn}/*",
    ]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.github_deploy.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "tenant_lifecycle_recovery" {
  count    = var.enable_cold_region_recovery_profile ? 1 : 0
  provider = aws.cold_region_recovery

  bucket     = aws_s3_bucket.tenant_lifecycle_recovery[0].id
  policy     = data.aws_iam_policy_document.tenant_lifecycle_recovery[0].json
  depends_on = [aws_s3_bucket_public_access_block.tenant_lifecycle_recovery[0]]
}

resource "aws_s3_bucket_policy" "learning_evidence" {
  count = var.enable_learning_infrastructure ? 1 : 0

  bucket     = aws_s3_bucket.learning_evidence[0].id
  policy     = data.aws_iam_policy_document.learning_evidence_bucket[0].json
  depends_on = [aws_s3_bucket_public_access_block.learning_evidence[0]]
}

moved {
  from = aws_s3_bucket.learning_evidence
  to   = aws_s3_bucket.learning_evidence[0]
}

moved {
  from = aws_kms_key.learning_corpus
  to   = aws_kms_key.learning_corpus[0]
}

moved {
  from = aws_kms_key.learning_qualification_hmac
  to   = aws_kms_key.learning_qualification_hmac[0]
}

moved {
  from = aws_kms_alias.learning_corpus
  to   = aws_kms_alias.learning_corpus[0]
}

moved {
  from = aws_kms_alias.learning_qualification_hmac
  to   = aws_kms_alias.learning_qualification_hmac[0]
}

moved {
  from = aws_s3_bucket_versioning.learning_evidence
  to   = aws_s3_bucket_versioning.learning_evidence[0]
}

moved {
  from = aws_s3_bucket_server_side_encryption_configuration.learning_evidence
  to   = aws_s3_bucket_server_side_encryption_configuration.learning_evidence[0]
}

moved {
  from = aws_s3_bucket_public_access_block.learning_evidence
  to   = aws_s3_bucket_public_access_block.learning_evidence[0]
}

moved {
  from = aws_s3_bucket_ownership_controls.learning_evidence
  to   = aws_s3_bucket_ownership_controls.learning_evidence[0]
}

moved {
  from = aws_s3_bucket_object_lock_configuration.learning_evidence
  to   = aws_s3_bucket_object_lock_configuration.learning_evidence[0]
}

moved {
  from = aws_iam_role.github_evidence
  to   = aws_iam_role.github_evidence[0]
}

moved {
  from = aws_iam_role_policy.github_evidence
  to   = aws_iam_role_policy.github_evidence[0]
}

moved {
  from = aws_s3_bucket_policy.learning_evidence
  to   = aws_s3_bucket_policy.learning_evidence[0]
}
