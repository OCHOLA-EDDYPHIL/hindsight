locals {
  state_bucket_arn     = data.aws_s3_bucket.state.arn
  evidence_bucket_name = "hindsight-${var.stage}-learning-evidence-${data.aws_caller_identity.current.account_id}"
  oidc_arn             = var.create_github_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : var.existing_github_oidc_provider_arn
  parameter_arn        = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/hindsight/${var.stage}/*"
  evidence_parameter_arns = [
    "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/hindsight/${var.stage}/database-url",
    "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/hindsight/${var.stage}/gemini-api-keys",
  ]
  lambda_version_refresh_actions = ["lambda:ListVersionsByFunction"]
  lambda_function_arns = [
    for component in ["api", "worker", "websocket", "changefeed"] :
    "arn:${data.aws_partition.current.partition}:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:hindsight-${var.stage}-${component}"
  ]
}

data "aws_s3_bucket" "state" {
  bucket = var.state_bucket_name
}

resource "aws_s3_bucket" "learning_evidence" {
  bucket              = local.evidence_bucket_name
  object_lock_enabled = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_key" "learning_qualification_hmac" {
  description              = "Hindsight learning qualification opaque identifiers"
  key_usage                = "GENERATE_VERIFY_MAC"
  customer_master_key_spec = "HMAC_256"
  deletion_window_in_days  = 30
  enable_key_rotation      = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "learning_qualification_hmac" {
  name          = "alias/hindsight-${var.stage}-learning-qualification-hmac"
  target_key_id = aws_kms_key.learning_qualification_hmac.key_id
}

resource "aws_s3_bucket_versioning" "learning_evidence" {
  bucket = aws_s3_bucket.learning_evidence.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "learning_evidence" {
  bucket = aws_s3_bucket.learning_evidence.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "learning_evidence" {
  bucket                  = aws_s3_bucket.learning_evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "learning_evidence" {
  bucket = aws_s3_bucket.learning_evidence.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "learning_evidence" {
  bucket = aws_s3_bucket.learning_evidence.id
  rule {
    default_retention {
      mode  = "GOVERNANCE"
      years = 7
    }
  }
  depends_on = [aws_s3_bucket_versioning.learning_evidence]
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
  name                 = "hindsight-github-evidence"
  assume_role_policy   = data.aws_iam_policy_document.github_assume.json
  max_session_duration = 3600
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
      values   = [var.application_state_key, "${var.application_state_key}.*"]
    }
  }

  statement {
    sid = "TerraformStateObject"
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:PutObject"
    ]
    resources = [
      "${local.state_bucket_arn}/${var.application_state_key}",
      "${local.state_bucket_arn}/${var.application_state_key}.*"
    ]
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

  statement {
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
      aws_s3_bucket.learning_evidence.arn,
      "${aws_s3_bucket.learning_evidence.arn}/*",
    ]
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
      "sts:GetCallerIdentity"
    ]
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

data "aws_iam_policy_document" "github_evidence" {
  statement {
    sid       = "EvidenceSettings"
    actions   = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = local.evidence_parameter_arns
  }

  statement {
    sid = "QualificationOpaqueIdentifiers"
    actions = [
      "kms:DescribeKey",
      "kms:GenerateMac",
      "kms:VerifyMac",
    ]
    resources = [aws_kms_key.learning_qualification_hmac.arn]
  }

  statement {
    sid = "EvidenceBucketMetadata"
    actions = [
      "s3:GetBucketLocation",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketVersioning",
    ]
    resources = [aws_s3_bucket.learning_evidence.arn]
  }

  statement {
    sid = "EvidenceBucketList"
    actions = [
      "s3:ListBucket",
      "s3:ListBucketVersions",
    ]
    resources = [aws_s3_bucket.learning_evidence.arn]
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
    resources = ["${aws_s3_bucket.learning_evidence.arn}/learning/*"]
  }
}

resource "aws_iam_role_policy" "github_evidence" {
  role   = aws_iam_role.github_evidence.id
  policy = data.aws_iam_policy_document.github_evidence.json
}

data "aws_iam_policy_document" "learning_evidence_bucket" {
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
      aws_s3_bucket.learning_evidence.arn,
      "${aws_s3_bucket.learning_evidence.arn}/*",
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
    resources = ["${aws_s3_bucket.learning_evidence.arn}/*"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.github_deploy.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "learning_evidence" {
  bucket     = aws_s3_bucket.learning_evidence.id
  policy     = data.aws_iam_policy_document.learning_evidence_bucket.json
  depends_on = [aws_s3_bucket_public_access_block.learning_evidence]
}
