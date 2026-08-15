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

mock_provider "aws" {
  alias = "cold_region_recovery"
}

run "isolated_lifecycle" {
  command = plan

  variables {
    expected_aws_account_id     = "123456789012"
    github_oidc_provider_arn    = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    github_deploy_role_arn      = "arn:aws:iam::123456789012:role/hindsight-github-deploy"
    bootstrap_state_bucket_name = "home-in-cloud-terraform-state-123456789012-us-east-1"
    bootstrap_certificate_arn   = "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
    bootstrap_hmac_key_arn      = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    bootstrap_apply_external_id = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }

  assert {
    condition     = aws_iam_role.github_lifecycle.name == "hindsight-github-lifecycle"
    error_message = "Tenant lifecycle operations must use a dedicated GitHub OIDC role."
  }

  assert {
    condition = (
      aws_iam_role.github_bootstrap_plan.name == "hindsight-github-bootstrap-plan" &&
      aws_iam_role.github_bootstrap_plan.assume_role_policy == data.aws_iam_policy_document.github_bootstrap_plan_assume.json &&
      length([
        for statement in data.aws_iam_policy_document.github_bootstrap_plan_assume.statement : statement
        if toset(statement.actions) == toset(["sts:AssumeRoleWithWebIdentity"]) &&
        statement.effect == null &&
        statement.not_actions == null &&
        statement.resources == null &&
        statement.not_resources == null &&
        length(statement.principals) == 1 &&
        length(statement.not_principals) == 0 &&
        one(statement.principals).type == "Federated" &&
        toset(one(statement.principals).identifiers) == toset([
          "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com",
        ]) &&
        length(statement.condition) == 2 &&
        length([
          for trust_condition in statement.condition : trust_condition
          if trust_condition.test == "StringEquals" &&
          trust_condition.variable == "token.actions.githubusercontent.com:aud" &&
          toset(trust_condition.values) == toset(["sts.amazonaws.com"])
        ]) == 1 &&
        length([
          for trust_condition in statement.condition : trust_condition
          if trust_condition.test == "StringEquals" &&
          trust_condition.variable == "token.actions.githubusercontent.com:sub" &&
          toset(trust_condition.values) == toset([
            "repo:OCHOLA-EDDYPHIL/hindsight:environment:demo",
          ])
        ]) == 1
      ]) == 1
    )
    error_message = "Bootstrap planning must use the exact repository, protected environment, audience, and account OIDC trust."
  }

  assert {
    condition = (
      aws_iam_role.github_bootstrap_apply.name == "hindsight-github-bootstrap-apply" &&
      aws_iam_role.github_bootstrap_apply.assume_role_policy == data.aws_iam_policy_document.github_bootstrap_apply_assume.json &&
      length([
        for statement in data.aws_iam_policy_document.github_bootstrap_apply_assume.statement : statement
        if toset(statement.actions) == toset(["sts:AssumeRole"]) &&
        statement.effect == null &&
        statement.not_actions == null &&
        statement.resources == null &&
        statement.not_resources == null &&
        length(statement.principals) == 1 &&
        length(statement.not_principals) == 0 &&
        one(statement.principals).type == "AWS" &&
        toset(one(statement.principals).identifiers) == toset([
          "arn:aws:iam::123456789012:role/hindsight-github-bootstrap-plan",
        ]) &&
        length(statement.condition) == 1 &&
        one(statement.condition).test == "StringEquals" &&
        one(statement.condition).variable == "sts:ExternalId" &&
        toset(one(statement.condition).values) == toset([
          "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        ])
      ]) == 1
    )
    error_message = "Bootstrap apply trust must allow only the bootstrap plan role with the exact external ID."
  }

  assert {
    condition = (
      {
        for statement in data.aws_iam_policy_document.github_bootstrap_plan.statement :
        statement.sid => {
          actions   = toset(statement.actions)
          resources = toset(statement.resources)
        }
        } == {
        CallerIdentity = {
          actions   = toset(["sts:GetCallerIdentity"])
          resources = toset(["*"])
        }
        BootstrapStateRead = {
          actions = toset(["s3:GetObject"])
          resources = toset([
            "arn:aws:s3:::home-in-cloud-terraform-state-123456789012-us-east-1/hindsight/bootstrap/terraform.tfstate",
          ])
        }
        BootstrapStateList = {
          actions = toset(["s3:ListBucket"])
          resources = toset([
            "arn:aws:s3:::home-in-cloud-terraform-state-123456789012-us-east-1",
          ])
        }
        BootstrapStateLock = {
          actions = toset([
            "s3:DeleteObject",
            "s3:GetObject",
            "s3:PutObject",
          ])
          resources = toset([
            "arn:aws:s3:::home-in-cloud-terraform-state-123456789012-us-east-1/hindsight/bootstrap/terraform.tfstate.tflock",
          ])
        }
        BootstrapArchiveRead = {
          actions = toset([
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
          ])
          resources = toset([
            "arn:aws:s3:::hindsight-demo-learning-evidence-123456789012",
          ])
        }
        BootstrapRoleRead = {
          actions = toset([
            "iam:GetRole",
            "iam:GetRolePolicy",
            "iam:ListAttachedRolePolicies",
            "iam:ListRolePolicies",
            "iam:ListRoleTags",
          ])
          resources = toset([
            "arn:aws:iam::123456789012:role/hindsight-github-deploy",
            "arn:aws:iam::123456789012:role/hindsight-github-evidence",
            "arn:aws:iam::123456789012:role/hindsight-github-observability-evidence",
            "arn:aws:iam::123456789012:role/hindsight-github-quarantine-redrive",
            "arn:aws:iam::123456789012:role/hindsight-github-worker-acceptance",
          ])
        }
        BootstrapManagedPolicyRead = {
          actions = toset([
            "iam:GetPolicy",
            "iam:GetPolicyVersion",
            "iam:ListPolicyTags",
          ])
          resources = toset([
            "arn:aws:iam::123456789012:policy/hindsight-github-deploy-encryption",
            "arn:aws:iam::123456789012:policy/hindsight-github-deploy-observability",
          ])
        }
        BootstrapCertificateRead = {
          actions = toset([
            "acm:DescribeCertificate",
            "acm:ListTagsForCertificate",
          ])
          resources = toset([
            "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000",
          ])
        }
        BootstrapKeyRead = {
          actions = toset([
            "kms:DescribeKey",
            "kms:GetKeyPolicy",
            "kms:GetKeyRotationStatus",
            "kms:ListResourceTags",
          ])
          resources = toset([
            "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000",
          ])
        }
        BootstrapAliasRead = {
          actions   = toset(["kms:ListAliases"])
          resources = toset(["*"])
        }
      } &&
      alltrue([
        for statement in data.aws_iam_policy_document.github_bootstrap_plan.statement :
        statement.effect == null &&
        statement.not_actions == null &&
        statement.not_resources == null &&
        length(statement.principals) == 0 &&
        length(statement.not_principals) == 0
      ])
    )
    error_message = "Bootstrap planning must expose only the exact reviewed action and resource allowlist."
  }

  assert {
    condition = (
      length(one([
        for statement in data.aws_iam_policy_document.github_bootstrap_plan.statement : statement
        if statement.sid == "BootstrapStateList"
      ]).condition) == 1 &&
      one(one([
        for statement in data.aws_iam_policy_document.github_bootstrap_plan.statement : statement
        if statement.sid == "BootstrapStateList"
      ]).condition).test == "StringEquals" &&
      one(one([
        for statement in data.aws_iam_policy_document.github_bootstrap_plan.statement : statement
        if statement.sid == "BootstrapStateList"
      ]).condition).variable == "s3:prefix" &&
      toset(one(one([
        for statement in data.aws_iam_policy_document.github_bootstrap_plan.statement : statement
        if statement.sid == "BootstrapStateList"
        ]).condition).values) == toset([
        "env:/",
        "hindsight/bootstrap/terraform.tfstate",
      ]) &&
      length([
        for statement in data.aws_iam_policy_document.github_bootstrap_plan.statement : statement
        if statement.sid != "BootstrapStateList" && length(statement.condition) > 0
      ]) == 0
    )
    error_message = "Bootstrap state listing must be limited to the exact state key and Terraform's default-workspace enumeration prefix."
  }

  assert {
    condition = (
      aws_iam_role_policy.github_bootstrap_plan.policy == data.aws_iam_policy_document.github_bootstrap_plan.json &&
      aws_iam_role_policy.github_bootstrap_apply_transition.name == "hindsight-github-bootstrap-apply-transition" &&
      aws_iam_role_policy.github_bootstrap_apply_transition.policy == data.aws_iam_policy_document.github_bootstrap_apply_transition.json &&
      length(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement) == 1 &&
      one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).sid == "BootstrapApplyTransition" &&
      toset(one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).actions) == toset(["sts:AssumeRole"]) &&
      toset(one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).resources) == toset([
        "arn:aws:iam::123456789012:role/hindsight-github-bootstrap-apply",
      ]) &&
      one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).effect == null &&
      one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).not_actions == null &&
      one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).not_resources == null &&
      length(one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).principals) == 0 &&
      length(one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).not_principals) == 0 &&
      length(one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).condition) == 1 &&
      one(one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).condition).test == "StringEquals" &&
      one(one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).condition).variable == "sts:ExternalId" &&
      toset(one(one(data.aws_iam_policy_document.github_bootstrap_apply_transition.statement).condition).values) == toset([
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      ])
    )
    error_message = "Bootstrap planning must keep reads separate from the exact external-ID-gated apply transition."
  }

  assert {
    condition = (
      aws_iam_role_policy.github_bootstrap_apply.policy == data.aws_iam_policy_document.github_bootstrap_apply.json &&
      toset(data.aws_iam_policy_document.github_bootstrap_apply.source_policy_documents) == toset([
        data.aws_iam_policy_document.github_bootstrap_plan.json,
      ]) &&
      {
        for statement in data.aws_iam_policy_document.github_bootstrap_apply.statement :
        statement.sid => {
          actions   = toset(statement.actions)
          resources = toset(statement.resources)
        }
        } == {
        BootstrapStateWrite = {
          actions = toset(["s3:PutObject"])
          resources = toset([
            "arn:aws:s3:::home-in-cloud-terraform-state-123456789012-us-east-1/hindsight/bootstrap/terraform.tfstate",
          ])
        }
        BootstrapRoleCreate = {
          actions = toset([
            "iam:CreateRole",
            "iam:TagRole",
          ])
          resources = toset([
            "arn:aws:iam::123456789012:role/hindsight-github-quarantine-redrive",
            "arn:aws:iam::123456789012:role/hindsight-github-worker-acceptance",
          ])
        }
        BootstrapInlinePolicyUpdate = {
          actions = toset(["iam:PutRolePolicy"])
          resources = toset([
            "arn:aws:iam::123456789012:role/hindsight-github-observability-evidence",
            "arn:aws:iam::123456789012:role/hindsight-github-quarantine-redrive",
            "arn:aws:iam::123456789012:role/hindsight-github-worker-acceptance",
          ])
        }
        BootstrapManagedPolicyCreate = {
          actions = toset([
            "iam:CreatePolicy",
            "iam:TagPolicy",
          ])
          resources = toset([
            "arn:aws:iam::123456789012:policy/hindsight-github-deploy-encryption",
          ])
        }
        BootstrapManagedPolicyVersionUpdate = {
          actions = toset([
            "iam:CreatePolicyVersion",
            "iam:DeletePolicyVersion",
            "iam:ListPolicyVersions",
          ])
          resources = toset([
            "arn:aws:iam::123456789012:policy/hindsight-github-deploy-encryption",
            "arn:aws:iam::123456789012:policy/hindsight-github-deploy-observability",
          ])
        }
        BootstrapManagedPolicyAttach = {
          actions = toset(["iam:AttachRolePolicy"])
          resources = toset([
            "arn:aws:iam::123456789012:role/hindsight-github-deploy",
          ])
        }
      } &&
      alltrue([
        for statement in data.aws_iam_policy_document.github_bootstrap_apply.statement :
        statement.effect == null &&
        statement.not_actions == null &&
        statement.not_resources == null &&
        length(statement.principals) == 0 &&
        length(statement.not_principals) == 0
      ]) &&
      length([
        for statement in data.aws_iam_policy_document.github_bootstrap_apply.statement : statement
        if statement.sid != "BootstrapManagedPolicyAttach" && length(statement.condition) > 0
      ]) == 0 &&
      length(one([
        for statement in data.aws_iam_policy_document.github_bootstrap_apply.statement : statement
        if statement.sid == "BootstrapManagedPolicyAttach"
      ]).condition) == 1 &&
      one(one([
        for statement in data.aws_iam_policy_document.github_bootstrap_apply.statement : statement
        if statement.sid == "BootstrapManagedPolicyAttach"
      ]).condition).test == "StringEquals" &&
      one(one([
        for statement in data.aws_iam_policy_document.github_bootstrap_apply.statement : statement
        if statement.sid == "BootstrapManagedPolicyAttach"
      ]).condition).variable == "iam:PolicyARN" &&
      toset(one(one([
        for statement in data.aws_iam_policy_document.github_bootstrap_apply.statement : statement
        if statement.sid == "BootstrapManagedPolicyAttach"
        ]).condition).values) == toset([
        "arn:aws:iam::123456789012:policy/hindsight-github-deploy-encryption",
      ])
    )
    error_message = "Bootstrap apply must inherit planning reads and add only the exact state and IAM mutations required by the reviewed exact-main plan."
  }

  assert {
    condition = (
      length(setintersection(
        toset(flatten([
          for statement in data.aws_iam_policy_document.github_bootstrap_apply.statement : statement.resources
        ])),
        toset([
          "arn:aws:iam::123456789012:role/hindsight-github-bootstrap-apply",
          "arn:aws:iam::123456789012:role/hindsight-github-bootstrap-plan",
        ])
      )) == 0 &&
      length(setintersection(
        toset(flatten([
          for statement in data.aws_iam_policy_document.github_bootstrap_apply.statement : statement.actions
        ])),
        toset([
          "iam:AddClientIDToOpenIDConnectProvider",
          "iam:CreateOpenIDConnectProvider",
          "iam:DeleteOpenIDConnectProvider",
          "iam:DeletePolicy",
          "iam:DeleteRole",
          "iam:DeleteRolePolicy",
          "iam:DetachRolePolicy",
          "iam:RemoveClientIDFromOpenIDConnectProvider",
          "iam:UpdateOpenIDConnectProviderThumbprint",
        ])
      )) == 0
    )
    error_message = "Bootstrap apply must not manage its lifecycle-owned identities or permit role, policy, attachment, or OIDC-provider destruction."
  }

  assert {
    condition = (
      aws_s3_bucket.tenant_lifecycle_exports.object_lock_enabled &&
      aws_s3_bucket_versioning.tenant_lifecycle_exports.versioning_configuration[0].status == "Enabled" &&
      one(one(aws_s3_bucket_server_side_encryption_configuration.tenant_lifecycle_exports.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256" &&
      one(aws_s3_bucket_ownership_controls.tenant_lifecycle_exports.rule).object_ownership == "BucketOwnerEnforced" &&
      aws_s3_bucket_public_access_block.tenant_lifecycle_exports.block_public_acls &&
      aws_s3_bucket_public_access_block.tenant_lifecycle_exports.block_public_policy &&
      aws_s3_bucket_public_access_block.tenant_lifecycle_exports.ignore_public_acls &&
      aws_s3_bucket_public_access_block.tenant_lifecycle_exports.restrict_public_buckets
    )
    error_message = "Tenant lifecycle exports must be immutable, versioned, encrypted, bucket-owner-enforced, and private."
  }

  assert {
    condition = (
      aws_s3_bucket_object_lock_configuration.tenant_lifecycle_exports.rule[0].default_retention[0].mode == "GOVERNANCE" &&
      aws_s3_bucket_object_lock_configuration.tenant_lifecycle_exports.rule[0].default_retention[0].days == 7 &&
      aws_s3_bucket_lifecycle_configuration.tenant_lifecycle_exports.rule[0].filter[0].prefix == "tenant-exports/" &&
      aws_s3_bucket_lifecycle_configuration.tenant_lifecycle_exports.rule[0].expiration[0].days == 8 &&
      aws_s3_bucket_lifecycle_configuration.tenant_lifecycle_exports.rule[0].noncurrent_version_expiration[0].noncurrent_days == 8
    )
    error_message = "Lifecycle export expiry must begin only after the seven-day Governance retention window."
  }

  assert {
    condition = length([
      for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
      if anytrue([
        for action in statement.actions :
        startswith(action, "s3:Delete") || action == "s3:BypassGovernanceRetention"
      ])
    ]) == 0
    error_message = "The lifecycle operator must not delete export objects or bypass retention."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "WriteAndVerifyLifecycleExports"
        ]).actions) == toset([
        "s3:AbortMultipartUpload",
        "s3:GetObject",
        "s3:GetObjectRetention",
        "s3:GetObjectVersion",
        "s3:ListMultipartUploadParts",
        "s3:PutObject",
        "s3:PutObjectRetention",
      ]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "WriteAndVerifyLifecycleExports"
      ]).resources) == toset(["${local.lifecycle_export_bucket_arn}/tenant-exports/*"])
    )
    error_message = "The lifecycle operator must be limited to multipart write and version verification under tenant-exports/."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "LifecycleExportBucketList"
      ]).actions) == toset(["s3:ListBucket", "s3:ListBucketVersions"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "LifecycleExportMultipartList"
      ]).actions) == toset(["s3:ListBucketMultipartUploads"])
    )
    error_message = "Lifecycle listing must keep prefix-aware object listing separate from bucket-scoped multipart listing."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantStateGet"
      ]).resources) == toset(local.lifecycle_table_arns) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantStateQuery"
      ]).resources) == toset(concat(local.lifecycle_table_arns, local.lifecycle_table_index_arns)) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantStateScan"
      ]).resources) == toset(local.lifecycle_table_arns) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantStateDelete"
      ]).resources) == toset(local.lifecycle_table_arns) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantRealtimeFence"
      ]).resources) == toset([local.lifecycle_connection_table_arn])
    )
    error_message = "Lifecycle DynamoDB access must be scoped to the three tenant tables and their tenant indexes."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantStateGet"
      ]).actions) == toset(["dynamodb:GetItem"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantStateQuery"
      ]).actions) == toset(["dynamodb:Query"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantStateScan"
      ]).actions) == toset(["dynamodb:Scan"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantStateDelete"
      ]).actions) == toset(["dynamodb:DeleteItem"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantRealtimeFence"
      ]).actions) == toset(["dynamodb:PutItem"])
    )
    error_message = "Lifecycle DynamoDB permissions must expose only scoped get, query, scan, delete, and realtime-fence writes."
  }

  assert {
    condition = (
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantIdentityCleanup"
      ]).actions) == toset(["cognito-idp:AdminDeleteUser", "cognito-idp:AdminGetUser"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantIdentityCleanup"
      ]).resources) == toset([local.lifecycle_cognito_user_pool_arn]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantWebSocketDisconnect"
      ]).actions) == toset(["execute-api:ManageConnections"]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "TenantWebSocketDisconnect"
      ]).resources) == toset([local.lifecycle_websocket_arn]) &&
      endswith(local.lifecycle_websocket_arn, "/${var.stage}/DELETE/@connections/*") &&
      toset(one([
        for statement in data.aws_iam_policy_document.github_lifecycle.statement : statement
        if statement.sid == "LifecycleDatabaseSettings"
      ]).resources) == toset([local.lifecycle_database_parameter_arn]) &&
      local.lifecycle_database_parameter_arn == "arn:aws:ssm:us-east-1:123456789012:parameter/hindsight/demo/lifecycle-database-url"
    )
    error_message = "Lifecycle identity, disconnect, and database access must remain account, stage, and resource scoped."
  }

  assert {
    condition = (
      length([
        for statement in data.aws_iam_policy_document.tenant_lifecycle_exports.statement : statement
        if statement.sid == "DenyInsecureTransport" && statement.effect == "Deny" && toset(statement.actions) == toset(["s3:*"])
      ]) == 1 &&
      length([
        for statement in data.aws_iam_policy_document.tenant_lifecycle_exports.statement : statement
        if statement.sid == "DenyDeploymentMutation" && statement.effect == "Deny"
      ]) == 1 &&
      toset(one([
        for statement in data.aws_iam_policy_document.tenant_lifecycle_exports.statement : statement
        if statement.sid == "DenyDeploymentMutation"
      ]).actions) == toset(local.lifecycle_archive_deploy_denied_actions) &&
      toset(one([
        for statement in data.aws_iam_policy_document.tenant_lifecycle_exports.statement : statement
        if statement.sid == "DenyDeploymentMutation"
        ]).resources) == toset([
        local.lifecycle_export_bucket_arn,
        "${local.lifecycle_export_bucket_arn}/*",
      ]) &&
      toset(one(one([
        for statement in data.aws_iam_policy_document.tenant_lifecycle_exports.statement : statement
        if statement.sid == "DenyDeploymentMutation"
      ]).principals).identifiers) == toset([var.github_deploy_role_arn])
    )
    error_message = "Lifecycle exports must deny insecure transport and deny deployment-role mutation."
  }

  assert {
    condition = (
      output.tenant_lifecycle_export_bucket == aws_s3_bucket.tenant_lifecycle_exports.bucket &&
      output.tenant_lifecycle_export_bucket_arn == local.lifecycle_export_bucket_arn &&
      output.lifecycle_database_url_parameter_name == "/hindsight/demo/lifecycle-database-url" &&
      output.lifecycle_database_url_parameter_arn == local.lifecycle_database_parameter_arn &&
      output.cold_region_lifecycle_export_bucket == null &&
      output.cold_region_lifecycle_export_bucket_arn == null &&
      output.cold_region_recovery_profile == null
    )
    error_message = "Lifecycle outputs must expose the operator boundary while keeping cold-region recovery disabled by default."
  }

  assert {
    condition = (
      length(aws_s3_bucket.tenant_lifecycle_recovery) == 0 &&
      length(aws_s3_bucket_versioning.tenant_lifecycle_recovery) == 0 &&
      length(aws_s3_bucket_object_lock_configuration.tenant_lifecycle_recovery) == 0 &&
      length(aws_s3_bucket_server_side_encryption_configuration.tenant_lifecycle_recovery) == 0 &&
      length(aws_s3_bucket_public_access_block.tenant_lifecycle_recovery) == 0 &&
      length(aws_s3_bucket_ownership_controls.tenant_lifecycle_recovery) == 0 &&
      length(aws_s3_bucket_policy.tenant_lifecycle_recovery) == 0 &&
      length(aws_iam_role.lifecycle_export_replication) == 0 &&
      length(aws_iam_role_policy.lifecycle_export_replication) == 0 &&
      length(aws_s3_bucket_replication_configuration.tenant_lifecycle_exports) == 0
    )
    error_message = "The default lifecycle root must create zero cold-region recovery resources."
  }

  assert {
    condition     = toset(var.github_subjects) == toset(["repo:OCHOLA-EDDYPHIL/hindsight:environment:demo"])
    error_message = "OIDC trust must remain scoped to the demo environment."
  }
}

run "cold_region_recovery_profile" {
  command = plan

  variables {
    expected_aws_account_id             = "123456789012"
    github_oidc_provider_arn            = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    github_deploy_role_arn              = "arn:aws:iam::123456789012:role/hindsight-github-deploy"
    bootstrap_state_bucket_name         = "home-in-cloud-terraform-state-123456789012-us-east-1"
    bootstrap_certificate_arn           = "arn:aws:acm:us-east-1:123456789012:certificate/00000000-0000-0000-0000-000000000000"
    bootstrap_hmac_key_arn              = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
    bootstrap_apply_external_id         = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    enable_cold_region_recovery_profile = true
    cold_region_recovery_region         = "eu-west-1"
  }

  assert {
    condition = (
      length(aws_s3_bucket.tenant_lifecycle_recovery) == 1 &&
      length(aws_iam_role.lifecycle_export_replication) == 1 &&
      length(aws_s3_bucket_replication_configuration.tenant_lifecycle_exports) == 1 &&
      aws_s3_bucket.tenant_lifecycle_recovery[0].object_lock_enabled &&
      aws_s3_bucket_versioning.tenant_lifecycle_recovery[0].versioning_configuration[0].status == "Enabled" &&
      one(one(aws_s3_bucket_server_side_encryption_configuration.tenant_lifecycle_recovery[0].rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256" &&
      one(aws_s3_bucket_ownership_controls.tenant_lifecycle_recovery[0].rule).object_ownership == "BucketOwnerEnforced" &&
      aws_s3_bucket_public_access_block.tenant_lifecycle_recovery[0].block_public_acls &&
      aws_s3_bucket_public_access_block.tenant_lifecycle_recovery[0].block_public_policy &&
      aws_s3_bucket_public_access_block.tenant_lifecycle_recovery[0].ignore_public_acls &&
      aws_s3_bucket_public_access_block.tenant_lifecycle_recovery[0].restrict_public_buckets
    )
    error_message = "Opting in must create one private, encrypted, versioned, locked recovery bucket and its replication path."
  }

  assert {
    condition = (
      aws_s3_bucket_object_lock_configuration.tenant_lifecycle_recovery[0].rule[0].default_retention[0].mode == "GOVERNANCE" &&
      aws_s3_bucket_object_lock_configuration.tenant_lifecycle_recovery[0].rule[0].default_retention[0].days == 7 &&
      aws_s3_bucket_lifecycle_configuration.tenant_lifecycle_recovery[0].rule[0].expiration[0].days == 8 &&
      aws_s3_bucket_lifecycle_configuration.tenant_lifecycle_recovery[0].rule[0].noncurrent_version_expiration[0].noncurrent_days == 8 &&
      length([
        for statement in data.aws_iam_policy_document.tenant_lifecycle_recovery[0].statement : statement
        if statement.sid == "DenyInsecureTransport" && statement.effect == "Deny" && toset(statement.actions) == toset(["s3:*"])
      ]) == 1 &&
      length([
        for statement in data.aws_iam_policy_document.tenant_lifecycle_recovery[0].statement : statement
        if statement.sid == "DenyDeploymentMutation" && statement.effect == "Deny"
      ]) == 1 &&
      toset(one([
        for statement in data.aws_iam_policy_document.tenant_lifecycle_recovery[0].statement : statement
        if statement.sid == "DenyDeploymentMutation"
      ]).actions) == toset(local.lifecycle_archive_deploy_denied_actions) &&
      toset(one(one([
        for statement in data.aws_iam_policy_document.tenant_lifecycle_recovery[0].statement : statement
        if statement.sid == "DenyDeploymentMutation"
      ]).principals).identifiers) == toset([var.github_deploy_role_arn])
    )
    error_message = "Recovery exports must retain locked versions for seven days, expire afterward, deny insecure transport, and deny deployment mutation."
  }

  assert {
    condition = (
      one(aws_s3_bucket_replication_configuration.tenant_lifecycle_exports[0].rule).status == "Enabled" &&
      one(one(aws_s3_bucket_replication_configuration.tenant_lifecycle_exports[0].rule).filter).prefix == "tenant-exports/" &&
      one(one(aws_s3_bucket_replication_configuration.tenant_lifecycle_exports[0].rule).delete_marker_replication).status == "Disabled" &&
      one(one(aws_s3_bucket_replication_configuration.tenant_lifecycle_exports[0].rule).destination).bucket == local.lifecycle_recovery_bucket_arn &&
      toset(one([
        for statement in data.aws_iam_policy_document.lifecycle_export_replication[0].statement : statement
        if statement.sid == "ReadLockedSourceVersions"
        ]).actions) == toset([
        "s3:GetObjectLegalHold",
        "s3:GetObjectRetention",
        "s3:GetObjectVersionAcl",
        "s3:GetObjectVersionForReplication",
        "s3:GetObjectVersionTagging",
      ]) &&
      toset(one([
        for statement in data.aws_iam_policy_document.lifecycle_export_replication[0].statement : statement
        if statement.sid == "ReplicateLockedVersions"
      ]).actions) == toset(["s3:ReplicateObject", "s3:ReplicateTags"]) &&
      length([
        for statement in data.aws_iam_policy_document.lifecycle_export_replication[0].statement : statement
        if contains(statement.actions, "s3:ReplicateDelete") || contains(statement.actions, "s3:BypassGovernanceRetention")
      ]) == 0
    )
    error_message = "Cross-region replication must preserve locked object metadata without replicating deletes or bypassing retention."
  }

  assert {
    condition = (
      output.cold_region_recovery_profile.enabled == true &&
      output.cold_region_recovery_profile.primary_region == "us-east-1" &&
      output.cold_region_recovery_profile.recovery_region == "eu-west-1" &&
      output.cold_region_recovery_profile.lifecycle_export_bucket == aws_s3_bucket.tenant_lifecycle_exports.bucket &&
      output.cold_region_recovery_profile.recovery_export_bucket == aws_s3_bucket.tenant_lifecycle_recovery[0].bucket &&
      output.cold_region_recovery_profile.recovery_export_bucket_arn == local.lifecycle_recovery_bucket_arn &&
      output.cold_region_recovery_profile.replication_role_arn == local.lifecycle_replication_role_arn &&
      output.cold_region_recovery_profile.provisioning == "cross-region-replication" &&
      output.cold_region_lifecycle_export_bucket == aws_s3_bucket.tenant_lifecycle_recovery[0].bucket &&
      output.cold_region_lifecycle_export_bucket_arn == local.lifecycle_recovery_bucket_arn
    )
    error_message = "Opting in must expose the materialized source, destination, region, and replication role profile."
  }
}
