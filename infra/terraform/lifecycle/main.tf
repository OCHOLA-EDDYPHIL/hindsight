locals {
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
  lifecycle_database_parameter_arn = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.lifecycle_database_url_parameter_name}"
  lifecycle_table_arns = [
    for table in ["realtime-tickets", "websocket-subscriptions", "websocket-connections"] :
    "arn:${data.aws_partition.current.partition}:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/hindsight-${var.stage}-${table}"
  ]
  lifecycle_table_index_arns = [
    for table_arn in local.lifecycle_table_arns : "${table_arn}/index/tenant-id-index"
  ]
  lifecycle_connection_table_arn            = "arn:${data.aws_partition.current.partition}:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/hindsight-${var.stage}-websocket-connections"
  lifecycle_cognito_user_pool_arn           = "arn:${data.aws_partition.current.partition}:cognito-idp:${var.aws_region}:${data.aws_caller_identity.current.account_id}:userpool/*"
  lifecycle_websocket_arn                   = "arn:${data.aws_partition.current.partition}:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*/${var.stage}/DELETE/@connections/*"
  expected_oidc_provider_arn                = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"
  expected_deploy_role_arn                  = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/hindsight-github-deploy"
  bootstrap_state_key                       = "hindsight/bootstrap/terraform.tfstate"
  bootstrap_state_bucket_arn                = "arn:${data.aws_partition.current.partition}:s3:::${var.bootstrap_state_bucket_name}"
  bootstrap_state_object_arn                = "${local.bootstrap_state_bucket_arn}/${local.bootstrap_state_key}"
  bootstrap_state_lock_object_arn           = "${local.bootstrap_state_object_arn}.tflock"
  bootstrap_plan_role_arn                   = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/hindsight-github-bootstrap-plan"
  bootstrap_apply_role_arn                  = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/hindsight-github-bootstrap-apply"
  bootstrap_evidence_role_arn               = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/hindsight-github-evidence"
  bootstrap_observability_evidence_role_arn = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/hindsight-github-observability-evidence"
  bootstrap_quarantine_redrive_role_arn     = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/hindsight-github-quarantine-redrive"
  bootstrap_worker_acceptance_role_arn      = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/hindsight-github-worker-acceptance"
  bootstrap_role_arns = [
    local.expected_deploy_role_arn,
    local.bootstrap_evidence_role_arn,
    local.bootstrap_observability_evidence_role_arn,
    local.bootstrap_quarantine_redrive_role_arn,
    local.bootstrap_worker_acceptance_role_arn,
  ]
  bootstrap_observability_policy_arn = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:policy/hindsight-github-deploy-observability"
  bootstrap_encryption_policy_arn    = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:policy/hindsight-github-deploy-encryption"
  bootstrap_managed_policy_arns = [
    local.bootstrap_observability_policy_arn,
    local.bootstrap_encryption_policy_arn,
  ]
  bootstrap_apply_created_role_arns = [
    local.bootstrap_quarantine_redrive_role_arn,
    local.bootstrap_worker_acceptance_role_arn,
  ]
  bootstrap_apply_inline_policy_role_arns = [
    local.bootstrap_observability_evidence_role_arn,
    local.bootstrap_quarantine_redrive_role_arn,
    local.bootstrap_worker_acceptance_role_arn,
  ]
  bootstrap_evidence_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::hindsight-${var.stage}-learning-evidence-${data.aws_caller_identity.current.account_id}"
}

check "expected_aws_account" {
  assert {
    condition     = data.aws_caller_identity.current.account_id == var.expected_aws_account_id
    error_message = "Authenticated AWS account does not match expected_aws_account_id."
  }
}

check "lifecycle_database_parameter_stage" {
  assert {
    condition     = var.lifecycle_database_url_parameter_name == "/hindsight/${var.stage}/lifecycle-database-url"
    error_message = "lifecycle_database_url_parameter_name must match the selected lifecycle stage."
  }
}

check "bootstrap_trust_anchors" {
  assert {
    condition = (
      var.github_oidc_provider_arn == local.expected_oidc_provider_arn &&
      var.github_deploy_role_arn == local.expected_deploy_role_arn
    )
    error_message = "Lifecycle trust inputs must use the bootstrap-owned OIDC provider and deployment role in the selected account."
  }
}

check "bootstrap_state_bucket_scope" {
  assert {
    condition     = var.bootstrap_state_bucket_name == "home-in-cloud-terraform-state-${data.aws_caller_identity.current.account_id}-${var.aws_region}"
    error_message = "bootstrap_state_bucket_name must use the fixed account and region state-bucket convention."
  }
}

check "bootstrap_plan_read_anchors" {
  assert {
    condition = (
      startswith(
        var.bootstrap_certificate_arn,
        "arn:${data.aws_partition.current.partition}:acm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:certificate/"
      ) &&
      startswith(
        var.bootstrap_hmac_key_arn,
        "arn:${data.aws_partition.current.partition}:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:key/"
      )
    )
    error_message = "Bootstrap plan reads must stay in the selected account and region."
  }
}

check "cold_region_recovery_profile_regions" {
  assert {
    condition     = !var.enable_cold_region_recovery_profile || var.cold_region_recovery_region != var.aws_region
    error_message = "cold_region_recovery_region must differ from aws_region when the recovery profile is enabled."
  }
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

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.github_oidc_provider_arn]
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

data "aws_iam_policy_document" "github_bootstrap_plan_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [var.github_oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:OCHOLA-EDDYPHIL/hindsight:environment:demo"]
    }
  }
}

data "aws_iam_policy_document" "github_bootstrap_apply_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [local.bootstrap_plan_role_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.bootstrap_apply_external_id]
    }
  }
}

resource "aws_iam_role" "github_lifecycle" {
  name                 = "hindsight-github-lifecycle"
  assume_role_policy   = data.aws_iam_policy_document.github_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role" "github_bootstrap_plan" {
  name                 = "hindsight-github-bootstrap-plan"
  assume_role_policy   = data.aws_iam_policy_document.github_bootstrap_plan_assume.json
  max_session_duration = 3600
}

resource "aws_iam_role" "github_bootstrap_apply" {
  name                 = "hindsight-github-bootstrap-apply"
  assume_role_policy   = data.aws_iam_policy_document.github_bootstrap_apply_assume.json
  max_session_duration = 3600
  depends_on           = [aws_iam_role.github_bootstrap_plan]
}

data "aws_iam_policy_document" "github_bootstrap_plan" {
  statement {
    sid       = "CallerIdentity"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }

  statement {
    sid       = "BootstrapStateRead"
    actions   = ["s3:GetObject"]
    resources = [local.bootstrap_state_object_arn]
  }

  statement {
    sid       = "BootstrapStateList"
    actions   = ["s3:ListBucket"]
    resources = [local.bootstrap_state_bucket_arn]
    condition {
      test     = "StringEquals"
      variable = "s3:prefix"
      values = [
        local.bootstrap_state_key,
        "env:/",
      ]
    }
  }

  statement {
    sid = "BootstrapStateLock"
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [local.bootstrap_state_lock_object_arn]
  }

  statement {
    sid = "BootstrapArchiveRead"
    actions = [
      "s3:GetAccelerateConfiguration",
      "s3:GetBucketAcl",
      "s3:GetBucketCORS",
      "s3:GetBucketLocation",
      "s3:GetBucketLogging",
      "s3:GetBucketObjectLockConfiguration",
      "s3:GetBucketOwnershipControls",
      "s3:GetBucketPolicy",
      "s3:GetBucketPublicAccessBlock",
      "s3:GetBucketRequestPayment",
      "s3:GetBucketTagging",
      "s3:GetBucketVersioning",
      "s3:GetBucketWebsite",
      "s3:GetEncryptionConfiguration",
      "s3:GetLifecycleConfiguration",
      "s3:GetReplicationConfiguration",
      "s3:ListBucket",
    ]
    resources = [local.bootstrap_evidence_bucket_arn]
  }

  statement {
    sid = "BootstrapRoleRead"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListRolePolicies",
      "iam:ListRoleTags",
    ]
    resources = local.bootstrap_role_arns
  }

  statement {
    sid = "BootstrapManagedPolicyRead"
    actions = [
      "iam:GetPolicy",
      "iam:GetPolicyVersion",
      "iam:ListPolicyTags",
    ]
    resources = local.bootstrap_managed_policy_arns
  }

  statement {
    sid = "BootstrapCertificateRead"
    actions = [
      "acm:DescribeCertificate",
      "acm:ListTagsForCertificate",
    ]
    resources = [var.bootstrap_certificate_arn]
  }

  statement {
    sid = "BootstrapKeyRead"
    actions = [
      "kms:DescribeKey",
      "kms:GetKeyPolicy",
      "kms:GetKeyRotationStatus",
      "kms:ListResourceTags",
    ]
    resources = [var.bootstrap_hmac_key_arn]
  }

  statement {
    sid       = "BootstrapAliasRead"
    actions   = ["kms:ListAliases"]
    resources = ["*"]
  }
}

data "aws_iam_policy_document" "github_bootstrap_apply_transition" {
  statement {
    sid       = "BootstrapApplyTransition"
    actions   = ["sts:AssumeRole"]
    resources = [local.bootstrap_apply_role_arn]

    condition {
      test     = "StringEquals"
      variable = "sts:ExternalId"
      values   = [var.bootstrap_apply_external_id]
    }
  }
}

data "aws_iam_policy_document" "github_bootstrap_apply" {
  source_policy_documents = [data.aws_iam_policy_document.github_bootstrap_plan.json]

  statement {
    sid       = "BootstrapStateWrite"
    actions   = ["s3:PutObject"]
    resources = [local.bootstrap_state_object_arn]
  }

  statement {
    sid = "BootstrapRoleCreate"
    actions = [
      "iam:CreateRole",
      "iam:TagRole",
    ]
    resources = local.bootstrap_apply_created_role_arns
  }

  statement {
    sid       = "BootstrapInlinePolicyUpdate"
    actions   = ["iam:PutRolePolicy"]
    resources = local.bootstrap_apply_inline_policy_role_arns
  }

  statement {
    sid = "BootstrapManagedPolicyCreate"
    actions = [
      "iam:CreatePolicy",
      "iam:TagPolicy",
    ]
    resources = [local.bootstrap_encryption_policy_arn]
  }

  statement {
    sid = "BootstrapManagedPolicyVersionUpdate"
    actions = [
      "iam:CreatePolicyVersion",
      "iam:DeletePolicyVersion",
      "iam:ListPolicyVersions",
    ]
    resources = local.bootstrap_managed_policy_arns
  }

  statement {
    sid       = "BootstrapManagedPolicyAttach"
    actions   = ["iam:AttachRolePolicy"]
    resources = [local.expected_deploy_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PolicyARN"
      values   = [local.bootstrap_encryption_policy_arn]
    }
  }
}

resource "aws_iam_role_policy" "github_bootstrap_plan" {
  role   = aws_iam_role.github_bootstrap_plan.id
  policy = data.aws_iam_policy_document.github_bootstrap_plan.json

  lifecycle {
    precondition {
      condition     = length(regexall("\\S", data.aws_iam_policy_document.github_bootstrap_plan.json)) <= 10240
      error_message = "The bootstrap planning inline policy exceeds the IAM role-policy quota."
    }
  }
}

resource "aws_iam_role_policy" "github_bootstrap_apply_transition" {
  name   = "hindsight-github-bootstrap-apply-transition"
  role   = aws_iam_role.github_bootstrap_plan.id
  policy = data.aws_iam_policy_document.github_bootstrap_apply_transition.json

  lifecycle {
    precondition {
      condition     = length(regexall("\\S", data.aws_iam_policy_document.github_bootstrap_apply_transition.json)) <= 10240
      error_message = "The bootstrap apply transition inline policy exceeds the IAM role-policy quota."
    }
  }
}

resource "aws_iam_role_policy" "github_bootstrap_apply" {
  role   = aws_iam_role.github_bootstrap_apply.id
  policy = data.aws_iam_policy_document.github_bootstrap_apply.json

  lifecycle {
    precondition {
      condition     = length(regexall("\\S", data.aws_iam_policy_document.github_bootstrap_apply.json)) <= 10240
      error_message = "The bootstrap apply inline policy exceeds the IAM role-policy quota."
    }
  }
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
      identifiers = [var.github_deploy_role_arn]
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
      identifiers = [var.github_deploy_role_arn]
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
