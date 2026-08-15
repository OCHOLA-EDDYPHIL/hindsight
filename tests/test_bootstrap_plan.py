from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = "a" * 40
WORKFLOW_REF = (
    "OCHOLA-EDDYPHIL/hindsight/.github/workflows/plan-bootstrap.yml@refs/heads/main"
)


def _validator():
    path = ROOT / "scripts/validate_bootstrap_plan.py"
    spec = importlib.util.spec_from_file_location("validate_bootstrap_plan", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _resource(
    address: str,
    actions: list[str],
    *,
    mode: str = "managed",
    resource_type: str = "aws_iam_role",
    provider: str = "registry.terraform.io/hashicorp/aws",
    **change_fields,
):
    name = address.rsplit(".", 1)[1]
    return {
        "address": address,
        "mode": mode,
        "type": resource_type,
        "name": name,
        "provider_name": provider,
        "change": {
            "actions": actions,
            "before": {},
            "after": {},
            "before_sensitive": False,
            "after_sensitive": False,
            **change_fields,
        },
    }


def _value_resource(
    address: str,
    *,
    resource_type: str,
    values: dict,
    sensitive_values: dict,
    name: str | None = None,
    index: int | None = None,
):
    resource = {
        "address": address,
        "mode": "managed",
        "type": resource_type,
        "name": name or address.rsplit(".", 1)[1],
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "schema_version": 0,
        "values": values,
        "sensitive_values": sensitive_values,
    }
    if index is not None:
        resource["index"] = index
    return resource


def _plan(validator):
    return {
        "format_version": "1.2",
        "terraform_version": validator.TERRAFORM_VERSION,
        "variables": {},
        "planned_values": {"outputs": {}, "root_module": {}},
        "resource_changes": [
            _resource(
                "aws_iam_policy.github_deploy_encryption",
                ["create"],
                resource_type="aws_iam_policy",
            ),
            _resource(
                "aws_iam_policy.github_deploy_observability",
                ["update"],
                resource_type="aws_iam_policy",
            ),
            _resource("aws_iam_role.github_quarantine_redrive", ["create"]),
            _resource("aws_iam_role.github_worker_acceptance", ["create"]),
            _resource(
                "aws_iam_role_policy.github_observability_evidence",
                ["update"],
                resource_type="aws_iam_role_policy",
            ),
            _resource(
                "aws_iam_role_policy.github_quarantine_redrive",
                ["create"],
                resource_type="aws_iam_role_policy",
            ),
            _resource(
                "aws_iam_role_policy.github_worker_acceptance",
                ["create"],
                resource_type="aws_iam_role_policy",
            ),
            _resource(
                "aws_iam_role_policy_attachment.github_deploy_encryption",
                ["create"],
                resource_type="aws_iam_role_policy_attachment",
            ),
            _resource(
                "cloudflare_dns_record.acm_validation",
                ["no-op"],
                resource_type="cloudflare_dns_record",
                provider="registry.terraform.io/cloudflare/cloudflare",
            ),
        ],
        "resource_drift": [],
        "output_changes": {
            "github_quarantine_redrive_role_arn": {
                "actions": ["create"],
                "before": None,
                "after": "arn:quarantine-redrive",
                "before_sensitive": False,
                "after_sensitive": False,
            },
            "github_worker_acceptance_role_arn": {
                "actions": ["create"],
                "before": None,
                "after": "arn:worker-acceptance",
                "before_sensitive": False,
                "after_sensitive": False,
            },
        },
        "configuration": {"root_module": {}},
        "checks": [
            {
                "address": {
                    "kind": "check",
                    "name": name.removeprefix("check."),
                    "to_display": name,
                },
                "status": "pass",
                "instances": [
                    {"address": {"to_display": name}, "status": "pass"}
                ],
            }
            for name in sorted(validator.REQUIRED_CHECKS)
        ],
        "applyable": True,
        "complete": True,
        "errored": False,
    }


def _no_change_plan(validator):
    plan = _plan(validator)
    plan["resource_changes"] = [
        entry
        for entry in plan["resource_changes"]
        if entry["address"] == "cloudflare_dns_record.acm_validation"
    ]
    plan["resource_drift"] = []
    plan["output_changes"] = {}
    plan["applyable"] = False
    return plan


def _plan_with_null_sensitive_placeholders(validator):
    plan = _plan(validator)
    acm_values = _value_resource(
        "aws_acm_certificate.demo",
        resource_type="aws_acm_certificate",
        values={"private_key": None},
        sensitive_values={"private_key": True},
    )
    object_lock_values = _value_resource(
        "aws_s3_bucket_object_lock_configuration.learning_evidence[0]",
        resource_type="aws_s3_bucket_object_lock_configuration",
        name="learning_evidence",
        index=0,
        values={"token": None},
        sensitive_values={"token": True},
    )
    plan["planned_values"]["root_module"]["resources"] = [
        acm_values,
        object_lock_values,
    ]
    plan["prior_state"] = {
        "values": {
            "root_module": {
                "resources": json.loads(json.dumps([acm_values, object_lock_values])),
            }
        }
    }
    acm_change = _resource(
        "aws_acm_certificate.demo",
        ["no-op"],
        resource_type="aws_acm_certificate",
        before={"private_key": None},
        after={"private_key": None},
        before_sensitive={"private_key": True},
        after_sensitive={"private_key": True},
        after_unknown={},
    )
    object_lock_change = _resource(
        "aws_s3_bucket_object_lock_configuration.learning_evidence[0]",
        ["no-op"],
        resource_type="aws_s3_bucket_object_lock_configuration",
        before={"token": None},
        after={"token": None},
        before_sensitive={"token": True},
        after_sensitive={"token": True},
        after_unknown={"token": False},
    )
    object_lock_change.update({"index": 0, "name": "learning_evidence"})
    plan["resource_changes"].extend([acm_change, object_lock_change])
    return plan


def _resource_entry(plan, collection: str, address: str):
    return next(entry for entry in plan[collection] if entry["address"] == address)


def _plan_with_cloudflare_refresh_drift(validator):
    plan = _plan(validator)
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))
    desired = _resource_entry(
        plan, "resource_changes", "cloudflare_dns_record.acm_validation"
    )
    desired.update(
        {
            "address": address,
            "index": "hindsight.strathmoreedu.qzz.io",
            "name": "acm_validation",
        }
    )
    refreshed = {
        "content": "_validation.acm-validations.aws",
        "name": "_validation.hindsight.strathmoreedu.qzz.io",
        "type": "CNAME",
    }
    desired["change"].update(
        {
            "before": refreshed,
            "after": json.loads(json.dumps(refreshed)),
        }
    )
    drift = _resource(
        address,
        ["update"],
        resource_type="cloudflare_dns_record",
        provider=validator.CLOUDFLARE_PROVIDER,
        before={
            **refreshed,
            "content": "_validation.acm-validations.aws.",
            "name": "_validation.hindsight.strathmoreedu.qzz.io.",
        },
        after=json.loads(json.dumps(refreshed)),
    )
    drift.update(
        {
            "index": "hindsight.strathmoreedu.qzz.io",
            "name": "acm_validation",
        }
    )
    plan["resource_drift"].append(drift)
    return plan


GITHUB_DEPLOY_ROLE_ADDRESS = "aws_iam_role.github_deploy"
GITHUB_DEPLOY_POLICY_NAME = "terraform-96877ae1e0309d9aea1db9eeb4"
LIFECYCLE_EXPORT_ARNS = {
    "arn:aws:s3:::hindsight-demo-lifecycle-exports-762397612117",
    "arn:aws:s3:::hindsight-demo-lifecycle-exports-762397612117/*",
}
LIFECYCLE_RECOVERY_ARNS = {
    "arn:aws:s3:::hindsight-demo-recovery-762397612117",
    "arn:aws:s3:::hindsight-demo-recovery-762397612117/*",
}
LIFECYCLE_ARCHIVE_DENIED_ACTIONS = {
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
}
GITHUB_DEPLOY_POLICY_SIDS = {
    "ApplicationIam",
    "ApplicationLifecycle",
    "CertificateReadiness",
    "ChangefeedConfigurationRead",
    "CognitoUserPoolCreate",
    "ControlledIncidentTelemetryRead",
    "ControlledIncidentTelemetryWrite",
    "EvidenceArchiveMutationDenied",
    "LambdaVersionRefresh",
    "LifecycleArchiveMutationDenied",
    "ParameterReadiness",
    "TerraformStateBucketMetadata",
    "TerraformStateList",
    "TerraformStateObject",
}
GITHUB_DEPLOY_ROLE_VALUE_FIELDS = {
    "arn",
    "assume_role_policy",
    "create_date",
    "description",
    "force_detach_policies",
    "id",
    "inline_policy",
    "managed_policy_arns",
    "max_session_duration",
    "name",
    "name_prefix",
    "path",
    "permissions_boundary",
    "tags",
    "tags_all",
    "unique_id",
}
GITHUB_DEPLOY_ROLE_SENSITIVE_VALUES = {
    "inline_policy": [{}],
    "managed_policy_arns": [False],
    "tags": {},
    "tags_all": {},
}


def _github_deploy_policy(resources: set[str]):
    return {
        "Version": "2012-10-17",
        "Statement": [
            *[
                {
                    "Action": "s3:GetBucketLocation",
                    "Effect": "Allow",
                    "Resource": f"arn:aws:s3:::unchanged-{sid.lower()}",
                    "Sid": sid,
                }
                for sid in sorted(
                    GITHUB_DEPLOY_POLICY_SIDS
                    - {"LifecycleArchiveMutationDenied"}
                )
            ],
            {
                "Action": sorted(LIFECYCLE_ARCHIVE_DENIED_ACTIONS),
                "Effect": "Deny",
                "Resource": sorted(resources),
                "Sid": "LifecycleArchiveMutationDenied",
            },
        ],
    }


def _github_deploy_role_values(policy: dict):
    return {
        "arn": "arn:aws:iam::762397612117:role/hindsight-github-deploy",
        "assume_role_policy": "unchanged",
        "create_date": "unchanged",
        "description": "",
        "force_detach_policies": False,
        "id": "hindsight-github-deploy",
        "inline_policy": [
            {
                "name": GITHUB_DEPLOY_POLICY_NAME,
                "policy": json.dumps(policy, separators=(",", ":")),
            }
        ],
        "managed_policy_arns": [
            "arn:aws:iam::762397612117:policy/hindsight-github-deploy-observability"
        ],
        "max_session_duration": 3600,
        "name": "hindsight-github-deploy",
        "name_prefix": "",
        "path": "/",
        "permissions_boundary": "",
        "tags": {},
        "tags_all": {"ManagedBy": "terraform-bootstrap", "Project": "hindsight"},
        "unique_id": "unchanged",
    }


def _plan_with_github_deploy_role_refresh_drift(
    validator,
    *,
    before_resources: set[str] | None = None,
    after_resources: set[str] | None = None,
):
    plan = _plan(validator)
    before = _github_deploy_role_values(
        _github_deploy_policy(
            LIFECYCLE_EXPORT_ARNS
            if before_resources is None
            else before_resources
        )
    )
    refreshed = _github_deploy_role_values(
        _github_deploy_policy(
            LIFECYCLE_EXPORT_ARNS | LIFECYCLE_RECOVERY_ARNS
            if after_resources is None
            else after_resources
        )
    )
    sensitivity = json.loads(json.dumps(GITHUB_DEPLOY_ROLE_SENSITIVE_VALUES))
    identity = {
        "account_id": "762397612117",
        "name": "hindsight-github-deploy",
    }
    desired = _resource(
        GITHUB_DEPLOY_ROLE_ADDRESS,
        ["no-op"],
        before=json.loads(json.dumps(refreshed)),
        after=json.loads(json.dumps(refreshed)),
        before_identity=identity,
        after_identity=json.loads(json.dumps(identity)),
        before_sensitive=sensitivity,
        after_sensitive=json.loads(json.dumps(sensitivity)),
        after_unknown={},
    )
    drift = _resource(
        GITHUB_DEPLOY_ROLE_ADDRESS,
        ["update"],
        before=before,
        after=refreshed,
        before_sensitive=json.loads(json.dumps(sensitivity)),
        after_sensitive=json.loads(json.dumps(sensitivity)),
        after_unknown={},
    )
    policy_values = {
        "id": f"hindsight-github-deploy:{GITHUB_DEPLOY_POLICY_NAME}",
        "name": GITHUB_DEPLOY_POLICY_NAME,
        "name_prefix": "terraform-",
        "policy": refreshed["inline_policy"][0]["policy"],
        "role": "hindsight-github-deploy",
    }
    policy_identity = {
        "account_id": "762397612117",
        "name": GITHUB_DEPLOY_POLICY_NAME,
        "role": "hindsight-github-deploy",
    }
    policy_change = _resource(
        "aws_iam_role_policy.github_deploy",
        ["no-op"],
        resource_type="aws_iam_role_policy",
        before=policy_values,
        after=json.loads(json.dumps(policy_values)),
        before_identity=policy_identity,
        after_identity=json.loads(json.dumps(policy_identity)),
        before_sensitive={},
        after_sensitive={},
        after_unknown={},
    )
    plan["resource_changes"].extend([desired, policy_change])
    plan["resource_drift"].append(drift)
    return plan


def _inline_policy_document(entry: dict, value: str) -> dict:
    policy = entry["change"][value]["inline_policy"][0]["policy"]
    return json.loads(policy)


def _set_inline_policy_document(entry: dict, value: str, policy: dict) -> None:
    entry["change"][value]["inline_policy"][0]["policy"] = json.dumps(
        policy, separators=(",", ":")
    )


def _lifecycle_statement(policy: dict) -> dict:
    return next(
        statement
        for statement in policy["Statement"]
        if statement.get("Sid") == "LifecycleArchiveMutationDenied"
    )


def _provenance(*, serial: int = 25):
    return {
        "lineage": "9f1383b4-4dae-30e4-bdba-649bc9346bc3",
        "serial": serial,
        "terraform_version": "1.15.8",
    }


def _receipt_inputs(
    directory: Path,
    validator,
    *,
    plan: dict | None = None,
    after_serial: int = 26,
):
    directory.mkdir()
    paths = {
        **{
            key: directory / name
            for key, name in validator.EXPECTED_ARTIFACT_NAMES.items()
        },
        **{
            key: directory / name
            for key, name in validator.EXPECTED_RECEIPT_NAMES.items()
        },
    }
    paths["plan"].write_bytes(b"saved terraform plan")
    paths["plan_json"].write_text(
        json.dumps(plan or _plan(validator)), encoding="utf-8"
    )
    paths["lock"].write_text("provider lock", encoding="utf-8")
    paths["state_before"].write_text(json.dumps(_provenance()), encoding="utf-8")
    paths["state_after"].write_text(json.dumps(_provenance()), encoding="utf-8")
    validator.build_manifest(
        plan_json=paths["plan_json"],
        plan_file=paths["plan"],
        lock_file=paths["lock"],
        state_before_file=paths["state_before"],
        state_after_file=paths["state_after"],
        actions_file=paths["actions"],
        manifest_file=paths["manifest"],
        source_revision=SOURCE_REVISION,
        repository=validator.EXPECTED_REPOSITORY,
        workflow_ref=WORKFLOW_REF,
        aws_account_id=validator.EXPECTED_AWS_ACCOUNT_ID,
        environment="demo",
        role_arn=(
            "arn:aws:iam::762397612117:role/hindsight-github-bootstrap-plan"
        ),
        run_id="1234",
        run_attempt="2",
    )
    paths["state_before_apply"].write_text(
        json.dumps(_provenance()), encoding="utf-8"
    )
    paths["state_after_apply"].write_text(
        json.dumps(_provenance(serial=after_serial)), encoding="utf-8"
    )
    paths["state_after_postcheck"].write_text(
        json.dumps(_provenance(serial=after_serial)), encoding="utf-8"
    )
    kwargs = {
        "manifest_file": paths["manifest"],
        "actions_file": paths["actions"],
        "state_before_apply_file": paths["state_before_apply"],
        "state_after_apply_file": paths["state_after_apply"],
        "state_after_postcheck_file": paths["state_after_postcheck"],
        "receipt_file": paths["receipt"],
        "source_revision": SOURCE_REVISION,
        "repository": validator.EXPECTED_REPOSITORY,
        "workflow_ref": WORKFLOW_REF,
        "aws_account_id": validator.EXPECTED_AWS_ACCOUNT_ID,
        "environment": "demo",
        "plan_role_arn": (
            "arn:aws:iam::762397612117:role/hindsight-github-bootstrap-plan"
        ),
        "apply_role_arn": (
            "arn:aws:iam::762397612117:role/hindsight-github-bootstrap-apply"
        ),
        "run_id": "1234",
        "run_attempt": "2",
        "plan_exit_code": "2",
        "postcheck_exit_code": "0",
    }
    return paths, kwargs


def _preapply_kwargs(paths: dict[str, Path], *, plan_exit_code: str = "2"):
    return {
        "manifest_file": paths["manifest"],
        "actions_file": paths["actions"],
        "plan_file": paths["plan"],
        "plan_json_file": paths["plan_json"],
        "lock_file": paths["lock"],
        "state_before_plan_file": paths["state_before"],
        "state_after_plan_file": paths["state_after"],
        "state_before_apply_file": paths["state_before_apply"],
        "plan_exit_code": plan_exit_code,
    }


def test_validator_records_the_exact_authorized_desired_state_mutations():
    validator = _validator()

    summary = validator.validate_plan(_plan(validator))

    assert [row["address"] for row in summary["resource_changes"]] == [
        "aws_iam_policy.github_deploy_encryption",
        "aws_iam_policy.github_deploy_observability",
        "aws_iam_role.github_quarantine_redrive",
        "aws_iam_role.github_worker_acceptance",
        "aws_iam_role_policy.github_observability_evidence",
        "aws_iam_role_policy.github_quarantine_redrive",
        "aws_iam_role_policy.github_worker_acceptance",
        "aws_iam_role_policy_attachment.github_deploy_encryption",
        "cloudflare_dns_record.acm_validation",
    ]
    assert summary["resource_drift"] == []
    assert [row["name"] for row in summary["output_changes"]] == [
        "github_quarantine_redrive_role_arn",
        "github_worker_acceptance_role_arn",
    ]
    assert summary["totals"] == {
        "checks": 2,
        "resource_changes": 9,
        "resource_drift": 0,
        "output_changes": 2,
    }
    assert summary["action_counts"] == {"create": 8, "no-op": 1, "update": 2}
    assert summary["observed_allowed_removals"] == {
        "data_resources": [],
        "outputs": [],
    }
    assert summary["null_sensitive_placeholders"] == []
    assert summary["reconciled_refresh_drift"] == []
    assert [row["to_display"] for row in summary["checks"]] == sorted(
        validator.REQUIRED_CHECKS
    )
    assert all(row["status"] == "pass" for row in summary["checks"])


def test_validator_allows_only_the_exact_resource_and_output_mutation_pairs():
    validator = _validator()
    expected_resources = {
        ("aws_iam_policy.github_deploy_encryption", ("create",)),
        ("aws_iam_policy.github_deploy_observability", ("update",)),
        ("aws_iam_role.github_quarantine_redrive", ("create",)),
        ("aws_iam_role.github_worker_acceptance", ("create",)),
        ("aws_iam_role_policy.github_observability_evidence", ("update",)),
        ("aws_iam_role_policy.github_quarantine_redrive", ("create",)),
        ("aws_iam_role_policy.github_worker_acceptance", ("create",)),
        ("aws_iam_role_policy_attachment.github_deploy_encryption", ("create",)),
    }
    expected_outputs = {
        ("github_quarantine_redrive_role_arn", ("create",)),
        ("github_worker_acceptance_role_arn", ("create",)),
    }

    assert validator.ALLOWED_RESOURCE_MUTATIONS == expected_resources
    assert validator.ALLOWED_OUTPUT_MUTATIONS == expected_outputs
    summary = validator.validate_plan(_plan(validator))
    assert {
        (entry["address"], tuple(entry["actions"]))
        for entry in summary["resource_changes"]
        if not validator.MUTATING_ACTIONS.isdisjoint(entry["actions"])
    } == expected_resources
    assert {
        (entry["name"], tuple(entry["actions"]))
        for entry in summary["output_changes"]
        if not validator.MUTATING_ACTIONS.isdisjoint(entry["actions"])
    } == expected_outputs


def test_validator_accepts_a_plan_with_no_desired_state_mutations():
    validator = _validator()

    summary = validator.validate_plan(_no_change_plan(validator))

    assert summary["applyable"] is False
    assert summary["action_counts"] == {"no-op": 1}
    assert summary["resource_drift"] == []


@pytest.mark.parametrize(
    ("address", "actions"),
    [
        ("aws_iam_policy.github_deploy_encryption", ["update"]),
        ("aws_iam_policy.github_deploy_observability", ["create"]),
        ("aws_iam_role.github_quarantine_redrive", ["update"]),
        ("aws_iam_role.github_worker_acceptance", ["update"]),
        ("aws_iam_role_policy.github_observability_evidence", ["create"]),
        ("aws_iam_role_policy.github_quarantine_redrive", ["update"]),
        ("aws_iam_role_policy.github_worker_acceptance", ["update"]),
        ("aws_iam_role_policy_attachment.github_deploy_encryption", ["update"]),
        ("aws_iam_role.unapproved", ["create"]),
        ("aws_iam_role.unapproved", ["update"]),
    ],
)
def test_validator_rejects_every_other_resource_mutation_pair(address, actions):
    validator = _validator()
    plan = _no_change_plan(validator)
    plan["resource_changes"].append(
        _resource(address, actions, resource_type=address.split(".", 1)[0])
    )
    plan["applyable"] = True

    with pytest.raises(ValueError, match="unapproved desired-state resource mutation"):
        validator.validate_plan(plan)


@pytest.mark.parametrize(
    ("name", "actions"),
    [
        ("github_quarantine_redrive_role_arn", ["update"]),
        ("github_worker_acceptance_role_arn", ["update"]),
        ("unapproved", ["create"]),
        ("unapproved", ["update"]),
    ],
)
def test_validator_rejects_every_other_output_mutation_pair(name, actions):
    validator = _validator()
    plan = _no_change_plan(validator)
    plan["output_changes"] = {
        name: {
            "actions": actions,
            "before": None,
            "after": "value",
            "before_sensitive": False,
            "after_sensitive": False,
        }
    }
    plan["applyable"] = True

    with pytest.raises(ValueError, match="unapproved desired-state output mutation"):
        validator.validate_plan(plan)


@pytest.mark.parametrize("actions", [["no-op"], ["read"], ["create"], ["update"]])
def test_validator_rejects_every_non_cloudflare_resource_drift(actions):
    validator = _validator()
    plan = _no_change_plan(validator)
    plan["resource_drift"] = [
        _resource("aws_iam_role.github_worker_acceptance", actions)
    ]

    with pytest.raises(ValueError, match="unapproved resource drift is forbidden"):
        validator.validate_plan(plan)


def test_validator_accepts_only_the_exact_github_deploy_role_refresh_transition():
    validator = _validator()

    summary = validator.validate_plan(
        _plan_with_github_deploy_role_refresh_drift(validator)
    )

    assert validator.LIFECYCLE_EXPORT_BUCKET_ARNS == LIFECYCLE_EXPORT_ARNS
    assert validator.LIFECYCLE_RECOVERY_BUCKET_ARNS == LIFECYCLE_RECOVERY_ARNS
    assert (
        validator.LIFECYCLE_ARCHIVE_DENIED_ACTIONS
        == LIFECYCLE_ARCHIVE_DENIED_ACTIONS
    )
    assert validator.GITHUB_DEPLOY_INLINE_POLICY_NAME == GITHUB_DEPLOY_POLICY_NAME
    assert validator.GITHUB_DEPLOY_POLICY_STATEMENT_SIDS == GITHUB_DEPLOY_POLICY_SIDS
    assert validator.GITHUB_DEPLOY_ROLE_VALUE_FIELDS == GITHUB_DEPLOY_ROLE_VALUE_FIELDS
    assert (
        validator.GITHUB_DEPLOY_ROLE_SENSITIVE_VALUES
        == GITHUB_DEPLOY_ROLE_SENSITIVE_VALUES
    )
    assert summary["reconciled_refresh_drift"] == [GITHUB_DEPLOY_ROLE_ADDRESS]
    role_drift = _resource_entry(
        summary, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    assert role_drift["actions"] == ["update"]


def test_validator_rejects_unrelated_drift_alongside_the_allowed_role_refresh():
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    plan["resource_drift"].append(
        _resource("aws_iam_role.unapproved", ["no-op"])
    )

    with pytest.raises(ValueError, match="unapproved resource drift is forbidden"):
        validator.validate_plan(plan)


@pytest.mark.parametrize("collection", ["resource_changes", "resource_drift"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "data"),
        ("name", "different"),
        ("provider_name", "registry.terraform.io/hashicorp/random"),
        ("type", "aws_iam_policy"),
        ("index", 0),
        ("deposed", "deadbeef"),
        ("module_address", "module.unapproved"),
        ("previous_address", "aws_iam_role.previous"),
        ("schema_version", 0),
    ],
)
def test_validator_rejects_github_deploy_role_refresh_identity_variants(
    collection, field, value
):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    _resource_entry(plan, collection, GITHUB_DEPLOY_ROLE_ADDRESS)[field] = value

    with pytest.raises(ValueError, match="GitHub deploy role refresh drift"):
        validator.validate_plan(plan)


@pytest.mark.parametrize("collection", ["resource_changes", "resource_drift"])
def test_validator_rejects_duplicate_github_deploy_role_entries(collection):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    plan[collection].append(
        json.loads(
            json.dumps(
                _resource_entry(plan, collection, GITHUB_DEPLOY_ROLE_ADDRESS)
            )
        )
    )

    with pytest.raises(ValueError, match="duplicate resource identities"):
        validator.validate_plan(plan)


def test_validator_requires_one_matching_github_deploy_role_desired_noop():
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    plan["resource_changes"] = [
        entry
        for entry in plan["resource_changes"]
        if entry["address"] != GITHUB_DEPLOY_ROLE_ADDRESS
    ]

    with pytest.raises(ValueError, match="not uniquely reconciled"):
        validator.validate_plan(plan)


@pytest.mark.parametrize(
    ("collection", "address", "message"),
    [
        (
            "resource_drift",
            GITHUB_DEPLOY_ROLE_ADDRESS,
            "not uniquely represented",
        ),
        (
            "resource_changes",
            GITHUB_DEPLOY_ROLE_ADDRESS,
            "not uniquely reconciled",
        ),
        (
            "resource_changes",
            "aws_iam_role_policy.github_deploy",
            "not bound to one inline policy",
        ),
    ],
)
def test_validator_custom_cardinality_rejects_distinct_deposed_duplicates(
    collection, address, message
):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    duplicate = json.loads(
        json.dumps(_resource_entry(plan, collection, address))
    )
    duplicate["deposed"] = "deadbeef"
    plan[collection].append(duplicate)

    with pytest.raises(ValueError, match=message):
        validator.validate_plan(plan)


@pytest.mark.parametrize("field", ["assume_role_policy", "tags_all", "unique_id"])
def test_validator_requires_the_exact_role_snapshot_fields(field):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(plan, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS)
    drift["change"]["after"].pop(field)

    with pytest.raises(ValueError, match="value identity is invalid"):
        validator.validate_plan(plan)


@pytest.mark.parametrize(
    ("collection", "actions"),
    [
        ("resource_changes", ["read"]),
        ("resource_changes", ["update"]),
        ("resource_drift", ["no-op"]),
        ("resource_drift", ["create"]),
    ],
)
def test_validator_rejects_other_github_deploy_role_refresh_actions(
    collection, actions
):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    _resource_entry(plan, collection, GITHUB_DEPLOY_ROLE_ADDRESS)["change"][
        "actions"
    ] = actions

    with pytest.raises(ValueError, match="GitHub deploy role refresh drift actions"):
        validator.validate_plan(plan)


@pytest.mark.parametrize(
    ("before_resources", "after_resources"),
    [
        (
            LIFECYCLE_EXPORT_ARNS | LIFECYCLE_RECOVERY_ARNS,
            LIFECYCLE_EXPORT_ARNS,
        ),
        (
            LIFECYCLE_EXPORT_ARNS,
            LIFECYCLE_EXPORT_ARNS
            | {"arn:aws:s3:::hindsight-demo-recovery-762397612117"},
        ),
        (
            LIFECYCLE_EXPORT_ARNS,
            LIFECYCLE_EXPORT_ARNS
            | LIFECYCLE_RECOVERY_ARNS
            | {"arn:aws:s3:::unapproved"},
        ),
        (
            LIFECYCLE_EXPORT_ARNS | LIFECYCLE_RECOVERY_ARNS,
            LIFECYCLE_EXPORT_ARNS | LIFECYCLE_RECOVERY_ARNS,
        ),
        (set(), LIFECYCLE_EXPORT_ARNS | LIFECYCLE_RECOVERY_ARNS),
    ],
)
def test_validator_rejects_reversed_partial_or_expanded_role_policy_transitions(
    before_resources, after_resources
):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(
        validator,
        before_resources=before_resources,
        after_resources=after_resources,
    )

    with pytest.raises(ValueError, match="target statement is invalid"):
        validator.validate_plan(plan)


@pytest.mark.parametrize("variant", ["missing", "replaced", "duplicate", "scalar"])
def test_validator_rejects_any_nonexact_lifecycle_deny_action_set(variant):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(plan, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS)
    policy = _inline_policy_document(drift, "after")
    statement = _lifecycle_statement(policy)
    if variant == "missing":
        statement["Action"].pop()
    elif variant == "replaced":
        statement["Action"] = ["s3:GetObject"]
    elif variant == "duplicate":
        statement["Action"].append(statement["Action"][0])
    else:
        statement["Action"] = "s3:DeleteBucket"
    _set_inline_policy_document(drift, "after", policy)

    with pytest.raises(ValueError, match="target statement is invalid"):
        validator.validate_plan(plan)


def test_validator_accepts_semantically_reordered_actions_and_arns():
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    role_desired = _resource_entry(
        plan, "resource_changes", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    role_drift = _resource_entry(plan, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS)
    policy_desired = _resource_entry(
        plan, "resource_changes", "aws_iam_role_policy.github_deploy"
    )
    for entry, value in (
        (role_desired, "before"),
        (role_desired, "after"),
        (role_drift, "before"),
        (role_drift, "after"),
    ):
        policy = _inline_policy_document(entry, value)
        statement = _lifecycle_statement(policy)
        statement["Action"].reverse()
        statement["Resource"].reverse()
        _set_inline_policy_document(entry, value, policy)
    for value in ("before", "after"):
        policy = json.loads(policy_desired["change"][value]["policy"])
        statement = _lifecycle_statement(policy)
        statement["Action"].reverse()
        statement["Resource"].reverse()
        policy_desired["change"][value]["policy"] = json.dumps(
            policy, separators=(",", ":")
        )

    summary = validator.validate_plan(plan)

    assert summary["reconciled_refresh_drift"] == [GITHUB_DEPLOY_ROLE_ADDRESS]


def test_validator_rejects_other_role_or_policy_changes_in_the_refresh():
    validator = _validator()

    role_change = _plan_with_github_deploy_role_refresh_drift(validator)
    _resource_entry(
        role_change, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS
    )["change"]["after"]["description"] = "changed"
    with pytest.raises(ValueError, match="changed other role values"):
        validator.validate_plan(role_change)

    statement_change = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(
        statement_change, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    policy = _inline_policy_document(drift, "after")
    policy["Statement"][0]["Action"] = "s3:GetObject"
    _set_inline_policy_document(drift, "after", policy)
    with pytest.raises(ValueError, match="changed other policy content"):
        validator.validate_plan(statement_change)

    action_change = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(
        action_change, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    policy = _inline_policy_document(drift, "after")
    _lifecycle_statement(policy)["Action"].append("s3:GetObject")
    _set_inline_policy_document(drift, "after", policy)
    with pytest.raises(ValueError, match="target statement is invalid"):
        validator.validate_plan(action_change)


def test_validator_rejects_extra_or_ambiguous_inline_policy_content():
    validator = _validator()

    extra_policy = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(
        extra_policy, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    drift["change"]["after"]["inline_policy"].append(
        json.loads(json.dumps(drift["change"]["after"]["inline_policy"][0]))
    )
    with pytest.raises(ValueError, match="one exact inline policy"):
        validator.validate_plan(extra_policy)

    extra_statement = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(
        extra_statement, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    policy = _inline_policy_document(drift, "after")
    policy["Statement"].append(json.loads(json.dumps(policy["Statement"][0])))
    policy["Statement"][-1]["Sid"] = "UnexpectedStatement"
    _set_inline_policy_document(drift, "after", policy)
    with pytest.raises(ValueError, match="policy is malformed"):
        validator.validate_plan(extra_statement)

    duplicate_target = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(
        duplicate_target, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    policy = _inline_policy_document(drift, "after")
    policy["Statement"][0]["Sid"] = "LifecycleArchiveMutationDenied"
    _set_inline_policy_document(drift, "after", policy)
    with pytest.raises(ValueError, match="statement identities are invalid"):
        validator.validate_plan(duplicate_target)

    duplicate_arn = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(
        duplicate_arn, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    policy = _inline_policy_document(drift, "after")
    _lifecycle_statement(policy)["Resource"].append(
        "arn:aws:s3:::hindsight-demo-recovery-762397612117"
    )
    _set_inline_policy_document(drift, "after", policy)
    with pytest.raises(ValueError, match="target statement is invalid"):
        validator.validate_plan(duplicate_arn)


def test_validator_rejects_a_self_consistent_generated_policy_name_mutation():
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    replacement = "terraform-" + "1" * 26
    role_desired = _resource_entry(
        plan, "resource_changes", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    role_drift = _resource_entry(plan, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS)
    for entry, value in (
        (role_desired, "before"),
        (role_desired, "after"),
        (role_drift, "before"),
        (role_drift, "after"),
    ):
        entry["change"][value]["inline_policy"][0]["name"] = replacement
    policy_change = _resource_entry(
        plan, "resource_changes", "aws_iam_role_policy.github_deploy"
    )
    for value in ("before", "after"):
        policy_change["change"][value]["name"] = replacement
        policy_change["change"][value]["id"] = (
            f"hindsight-github-deploy:{replacement}"
        )
    for identity in ("before_identity", "after_identity"):
        policy_change["change"][identity]["name"] = replacement

    with pytest.raises(ValueError, match="inline policy identity is invalid"):
        validator.validate_plan(plan)


def test_validator_rejects_a_non_target_sid_swap_with_fourteen_unique_sids():
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    role_desired = _resource_entry(
        plan, "resource_changes", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    role_drift = _resource_entry(plan, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS)
    for entry, value in (
        (role_desired, "before"),
        (role_desired, "after"),
        (role_drift, "before"),
        (role_drift, "after"),
    ):
        policy = _inline_policy_document(entry, value)
        next(
            statement
            for statement in policy["Statement"]
            if statement["Sid"] == "ApplicationIam"
        )["Sid"] = "UnexpectedApplicationIam"
        _set_inline_policy_document(entry, value, policy)
    policy_change = _resource_entry(
        plan, "resource_changes", "aws_iam_role_policy.github_deploy"
    )
    for value in ("before", "after"):
        policy = json.loads(policy_change["change"][value]["policy"])
        next(
            statement
            for statement in policy["Statement"]
            if statement["Sid"] == "ApplicationIam"
        )["Sid"] = "UnexpectedApplicationIam"
        assert len({statement["Sid"] for statement in policy["Statement"]}) == 14
        policy_change["change"][value]["policy"] = json.dumps(
            policy, separators=(",", ":")
        )

    with pytest.raises(ValueError, match="statement identities are invalid"):
        validator.validate_plan(plan)


@pytest.mark.parametrize("malformed", ["{", '{"Version":NaN}', '{"a":1,"a":2}'])
def test_validator_rejects_malformed_non_json_or_duplicate_policy_values(malformed):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(plan, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS)
    drift["change"]["after"]["inline_policy"][0]["policy"] = malformed

    with pytest.raises(ValueError, match="policy is malformed"):
        validator.validate_plan(plan)


@pytest.mark.parametrize("policy_value", [None, {}, []])
def test_validator_rejects_non_string_inline_policy_values(policy_value):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(plan, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS)
    drift["change"]["after"]["inline_policy"][0]["policy"] = policy_value

    with pytest.raises(ValueError, match="policy is malformed"):
        validator.validate_plan(plan)


@pytest.mark.parametrize("variant", ["duplicate-key", "nan"])
def test_validator_rejects_valid_shape_policy_json_edge_cases(variant):
    validator = _validator()
    plan = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(plan, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS)
    raw_policy = drift["change"]["after"]["inline_policy"][0]["policy"]
    if variant == "duplicate-key":
        raw_policy = raw_policy.replace(
            '"Effect":"Deny"',
            '"Effect":"Deny","Effect":"Deny"',
            1,
        )
    else:
        recovery_arn = json.dumps(
            "arn:aws:s3:::hindsight-demo-recovery-762397612117"
        )
        raw_policy = raw_policy.replace(recovery_arn, "NaN", 1)
    drift["change"]["after"]["inline_policy"][0]["policy"] = raw_policy

    with pytest.raises(ValueError, match="policy is malformed"):
        validator.validate_plan(plan)


def test_validator_rejects_unmatched_role_noop_and_refresh_metadata():
    validator = _validator()

    unmatched = _plan_with_github_deploy_role_refresh_drift(validator)
    desired = _resource_entry(
        unmatched, "resource_changes", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    desired["change"]["after"]["description"] = "different"
    with pytest.raises(ValueError, match="not a matching desired no-op"):
        validator.validate_plan(unmatched)

    stale_noop = _plan_with_github_deploy_role_refresh_drift(validator)
    desired = _resource_entry(
        stale_noop, "resource_changes", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    desired["change"]["before"]["description"] = "stale"
    desired["change"]["after"]["description"] = "stale"
    with pytest.raises(ValueError, match="not a matching desired no-op"):
        validator.validate_plan(stale_noop)

    unknown = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(unknown, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS)
    drift["change"]["after_unknown"] = {"inline_policy": True}
    with pytest.raises(ValueError, match="contains unknown values"):
        validator.validate_plan(unknown)

    importing = _plan_with_github_deploy_role_refresh_drift(validator)
    desired = _resource_entry(
        importing, "resource_changes", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    desired["change"]["importing"] = {"id": "unexpected"}
    with pytest.raises(ValueError, match="unexpected metadata"):
        validator.validate_plan(importing)

    identity = _plan_with_github_deploy_role_refresh_drift(validator)
    desired = _resource_entry(
        identity, "resource_changes", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    desired["change"]["after_identity"]["account_id"] = "000000000000"
    with pytest.raises(ValueError, match="change identity is invalid"):
        validator.validate_plan(identity)

    sensitivity = _plan_with_github_deploy_role_refresh_drift(validator)
    drift = _resource_entry(
        sensitivity, "resource_drift", GITHUB_DEPLOY_ROLE_ADDRESS
    )
    drift["change"]["after_sensitive"].pop("tags_all")
    with pytest.raises(ValueError, match="snapshot is malformed"):
        validator.validate_plan(sensitivity)


def test_validator_requires_one_matching_managed_inline_policy_noop():
    validator = _validator()
    policy_address = "aws_iam_role_policy.github_deploy"

    missing = _plan_with_github_deploy_role_refresh_drift(validator)
    missing["resource_changes"] = [
        entry
        for entry in missing["resource_changes"]
        if entry["address"] != policy_address
    ]
    with pytest.raises(ValueError, match="not bound to one inline policy"):
        validator.validate_plan(missing)

    duplicate = _plan_with_github_deploy_role_refresh_drift(validator)
    duplicate["resource_changes"].append(
        json.loads(
            json.dumps(_resource_entry(duplicate, "resource_changes", policy_address))
        )
    )
    with pytest.raises(ValueError, match="duplicate resource identities"):
        validator.validate_plan(duplicate)

    wrong_action = _plan_with_github_deploy_role_refresh_drift(validator)
    policy_change = _resource_entry(
        wrong_action, "resource_changes", policy_address
    )
    policy_change["change"]["actions"] = ["read"]
    with pytest.raises(ValueError, match="inline policy no-op is invalid"):
        validator.validate_plan(wrong_action)

    wrong_identity = _plan_with_github_deploy_role_refresh_drift(validator)
    policy_change = _resource_entry(
        wrong_identity, "resource_changes", policy_address
    )
    policy_change["change"]["after_identity"]["account_id"] = "000000000000"
    with pytest.raises(ValueError, match="policy change identity is invalid"):
        validator.validate_plan(wrong_identity)

    extra_metadata = _plan_with_github_deploy_role_refresh_drift(validator)
    policy_change = _resource_entry(
        extra_metadata, "resource_changes", policy_address
    )
    policy_change["change"]["importing"] = {"id": "unexpected"}
    with pytest.raises(ValueError, match="contains unexpected metadata"):
        validator.validate_plan(extra_metadata)

    changed = _plan_with_github_deploy_role_refresh_drift(validator)
    policy_change = _resource_entry(changed, "resource_changes", policy_address)
    policy_change["change"]["after"]["role"] = "different"
    with pytest.raises(ValueError, match="inline policy no-op is invalid"):
        validator.validate_plan(changed)

    mismatched = _plan_with_github_deploy_role_refresh_drift(validator)
    policy_change = _resource_entry(mismatched, "resource_changes", policy_address)
    policy = json.loads(policy_change["change"]["after"]["policy"])
    policy["Statement"][0]["Action"] = "s3:GetObject"
    policy_change["change"]["before"]["policy"] = json.dumps(
        policy, separators=(",", ":")
    )
    policy_change["change"]["after"]["policy"] = json.dumps(
        policy, separators=(",", ":")
    )
    with pytest.raises(ValueError, match="does not match its inline policy"):
        validator.validate_plan(mismatched)

    malformed = _plan_with_github_deploy_role_refresh_drift(validator)
    policy_change = _resource_entry(malformed, "resource_changes", policy_address)
    policy_change["change"]["before"]["policy"] = "{"
    policy_change["change"]["after"]["policy"] = "{"
    with pytest.raises(ValueError, match="policy is malformed"):
        validator.validate_plan(malformed)

    wrong_resource_identity = _plan_with_github_deploy_role_refresh_drift(validator)
    policy_change = _resource_entry(
        wrong_resource_identity, "resource_changes", policy_address
    )
    policy_change["provider_name"] = "registry.terraform.io/hashicorp/random"
    with pytest.raises(ValueError, match="inline policy identity is invalid"):
        validator.validate_plan(wrong_resource_identity)

    drifted = _plan_with_github_deploy_role_refresh_drift(validator)
    policy_change = _resource_entry(drifted, "resource_changes", policy_address)
    policy_drift = json.loads(json.dumps(policy_change))
    policy_drift["change"].pop("before_identity")
    policy_drift["change"].pop("after_identity")
    policy_drift["change"]["actions"] = ["update"]
    drifted["resource_drift"].append(policy_drift)
    with pytest.raises(ValueError, match="inline policy drift is forbidden"):
        validator.validate_plan(drifted)


def test_validator_records_only_exact_reconciled_cloudflare_refresh_drift():
    validator = _validator()
    plan = _plan_with_cloudflare_refresh_drift(validator)

    summary = validator.validate_plan(plan)

    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))
    assert summary["reconciled_refresh_drift"] == [address]
    cloudflare_drift = next(
        row for row in summary["resource_drift"] if row["address"] == address
    )
    assert cloudflare_drift["actions"] == ["update"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("mode", "data"),
        ("name", "different"),
        ("provider_name", "registry.terraform.io/hashicorp/aws"),
        ("type", "aws_route53_record"),
        ("index", "different.example"),
        ("deposed", "deadbeef"),
        ("module_address", "module.unapproved"),
        ("previous_address", "cloudflare_dns_record.previous"),
        ("schema_version", 0),
    ],
)
@pytest.mark.parametrize("collection", ["resource_changes", "resource_drift"])
def test_validator_rejects_cloudflare_refresh_drift_identity_variants(
    field, value, collection
):
    validator = _validator()
    plan = _plan_with_cloudflare_refresh_drift(validator)
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))
    _resource_entry(plan, collection, address)[field] = value

    with pytest.raises(ValueError, match="refresh drift identity"):
        validator.validate_plan(plan)


def test_validator_rejects_unapproved_or_uncorrelated_cloudflare_refresh_drift():
    validator = _validator()
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))

    unapproved = _plan_with_cloudflare_refresh_drift(validator)
    drift = _resource_entry(unapproved, "resource_drift", address)
    drift.update(
        {
            "address": 'cloudflare_dns_record.acm_validation["other.example"]',
            "index": "other.example",
        }
    )
    with pytest.raises(ValueError, match="unapproved Cloudflare refresh drift"):
        validator.validate_plan(unapproved)

    missing = _plan_with_cloudflare_refresh_drift(validator)
    missing["resource_changes"] = [
        entry for entry in missing["resource_changes"] if entry["address"] != address
    ]
    with pytest.raises(ValueError, match="not uniquely reconciled"):
        validator.validate_plan(missing)

    duplicate = _plan_with_cloudflare_refresh_drift(validator)
    duplicate["resource_changes"].append(
        json.loads(
            json.dumps(_resource_entry(duplicate, "resource_changes", address))
        )
    )
    with pytest.raises(ValueError, match="duplicate resource identities"):
        validator.validate_plan(duplicate)


def test_validator_rejects_cloudflare_drift_that_is_not_an_exact_noop():
    validator = _validator()
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))

    wrong_drift_action = _plan_with_cloudflare_refresh_drift(validator)
    _resource_entry(wrong_drift_action, "resource_drift", address)["change"][
        "actions"
    ] = ["no-op"]
    with pytest.raises(ValueError, match="not a no-op"):
        validator.validate_plan(wrong_drift_action)

    actual_change = _plan_with_cloudflare_refresh_drift(validator)
    _resource_entry(actual_change, "resource_changes", address)["change"][
        "actions"
    ] = ["update"]
    with pytest.raises(ValueError, match="Cloudflare managed changes"):
        validator.validate_plan(actual_change)

    unreconciled = _plan_with_cloudflare_refresh_drift(validator)
    _resource_entry(unreconciled, "resource_changes", address)["change"]["before"][
        "name"
    ] = "different.example"
    with pytest.raises(ValueError, match="not a no-op"):
        validator.validate_plan(unreconciled)

    forged_noop = _plan_with_cloudflare_refresh_drift(validator)
    _resource_entry(forged_noop, "resource_changes", address)["change"]["after"][
        "name"
    ] = "different.example"
    with pytest.raises(ValueError, match="not a no-op"):
        validator.validate_plan(forged_noop)

    type_confusion = _plan_with_cloudflare_refresh_drift(validator)
    drift_after = _resource_entry(
        type_confusion, "resource_drift", address
    )["change"]["after"]
    desired_change = _resource_entry(
        type_confusion, "resource_changes", address
    )["change"]
    drift_after["proxied"] = True
    desired_change["before"]["proxied"] = 1
    desired_change["after"]["proxied"] = 1
    with pytest.raises(ValueError, match="not a no-op"):
        validator.validate_plan(type_confusion)

    unchanged_actual = _plan_with_cloudflare_refresh_drift(validator)
    drift_change = _resource_entry(
        unchanged_actual, "resource_drift", address
    )["change"]
    drift_change["before"] = json.loads(json.dumps(drift_change["after"]))
    with pytest.raises(ValueError, match="not a no-op"):
        validator.validate_plan(unchanged_actual)


@pytest.mark.parametrize("collection", ["resource_changes", "resource_drift"])
@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("entry", "action_reason", "replace_because_tainted"),
        ("change", "action_reason", "replace_because_tainted"),
        ("change", "importing", {"id": "unexpected"}),
        ("change", "replace_paths", [["content"]]),
        ("change", "after_unknown", {"content": True}),
        ("change", "after_unknown", {"content": 1}),
        ("change", "after_unknown", {"content": "false"}),
    ],
)
def test_validator_rejects_additional_cloudflare_refresh_behavior(
    collection, location, field, value
):
    validator = _validator()
    plan = _plan_with_cloudflare_refresh_drift(validator)
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))
    entry = _resource_entry(plan, collection, address)
    target = entry if location == "entry" else entry["change"]
    target[field] = value

    expected_error = (
        "Cloudflare managed changes"
        if collection == "resource_changes"
        else "additional behavior"
    )
    with pytest.raises(ValueError, match=expected_error):
        validator.validate_plan(plan)


def test_validator_allows_only_recursively_clear_unknown_masks():
    validator = _validator()
    plan = _plan_with_cloudflare_refresh_drift(validator)
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))
    for collection in ("resource_changes", "resource_drift"):
        _resource_entry(plan, collection, address)["change"]["after_unknown"] = {
            "records": [{"content": False}],
        }

    summary = validator.validate_plan(plan)

    assert summary["reconciled_refresh_drift"] == [address]


def test_address_aware_guard_rejects_spoofed_cloudflare_action_identity():
    validator = _validator()
    plan = _plan_with_cloudflare_refresh_drift(validator)
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))
    plan["resource_drift"] = []
    change = _resource_entry(plan, "resource_changes", address)
    change.update(
        {
            "provider_name": "registry.terraform.io/hashicorp/aws",
            "type": "aws_route53_record",
        }
    )
    change["change"]["actions"] = ["update"]

    with pytest.raises(ValueError, match="Cloudflare managed changes"):
        validator.validate_plan(plan)

    data_spoof = _plan_with_cloudflare_refresh_drift(validator)
    data_spoof["resource_drift"] = []
    data_change = _resource_entry(data_spoof, "resource_changes", address)
    data_change.update(
        {
            "mode": "data",
            "provider_name": "registry.terraform.io/hashicorp/aws",
            "type": "aws_route53_record",
        }
    )
    data_change["change"]["actions"] = ["update"]
    with pytest.raises(ValueError, match="Cloudflare managed changes"):
        validator.validate_plan(data_spoof)

    hidden_import = _plan_with_cloudflare_refresh_drift(validator)
    hidden_import["resource_drift"] = []
    _resource_entry(hidden_import, "resource_changes", address)["change"][
        "importing"
    ] = {"id": "unexpected"}
    with pytest.raises(ValueError, match="Cloudflare managed changes"):
        validator.validate_plan(hidden_import)


def test_cloudflare_refresh_drift_errors_do_not_disclose_values():
    validator = _validator()
    plan = _plan_with_cloudflare_refresh_drift(validator)
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))
    sentinel = "must-never-appear-in-the-error"
    _resource_entry(plan, "resource_changes", address)["change"]["before"][
        "content"
    ] = sentinel

    with pytest.raises(ValueError, match="not a no-op") as raised:
        validator.validate_plan(plan)
    assert sentinel not in str(raised.value)


@pytest.mark.parametrize("non_json_number", [float("nan"), float("inf"), -float("inf")])
def test_validator_rejects_non_json_cloudflare_refresh_values(non_json_number):
    validator = _validator()
    plan = _plan_with_cloudflare_refresh_drift(validator)
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))
    _resource_entry(plan, "resource_drift", address)["change"]["before"][
        "ttl"
    ] = non_json_number

    with pytest.raises(ValueError, match="invalid JSON values"):
        validator.validate_plan(plan)


@pytest.mark.parametrize("status", ["fail", "error", "unknown"])
def test_validator_rejects_nonpassing_check_statuses(status):
    validator = _validator()
    aggregate = _plan(validator)
    aggregate["checks"][0]["status"] = status
    with pytest.raises(ValueError, match="check did not pass"):
        validator.validate_plan(aggregate)

    instance = _plan(validator)
    instance["checks"][0]["instances"][0]["status"] = status
    with pytest.raises(ValueError, match="instance did not pass"):
        validator.validate_plan(instance)


@pytest.mark.parametrize("checks", [None, [], {}, ["malformed"]])
def test_validator_rejects_incomplete_or_malformed_checks(checks):
    validator = _validator()
    plan = _plan(validator)
    plan["checks"] = checks
    with pytest.raises(ValueError, match="checks|check entries"):
        validator.validate_plan(plan)


def test_validator_rejects_missing_duplicate_and_problematic_checks():
    validator = _validator()
    missing = _plan(validator)
    missing["checks"] = missing["checks"][:1]
    with pytest.raises(ValueError, match="missing required checks"):
        validator.validate_plan(missing)

    duplicate = _plan(validator)
    duplicate["checks"].append(duplicate["checks"][0])
    with pytest.raises(ValueError, match="duplicate check identity"):
        validator.validate_plan(duplicate)

    problems = _plan(validator)
    problems["checks"][0]["instances"][0]["problems"] = [
        {"message": "must not be persisted"}
    ]
    with pytest.raises(ValueError, match="contains problems"):
        validator.validate_plan(problems)

    wrong_identity = _plan(validator)
    wrong_identity["checks"][0]["address"].update(
        {"name": "not_required", "to_display": "check.not_required"}
    )
    wrong_identity["checks"][0]["instances"][0]["address"][
        "to_display"
    ] = "check.not_required"
    with pytest.raises(ValueError, match="missing required checks"):
        validator.validate_plan(wrong_identity)

    aggregate_problems = _plan(validator)
    aggregate_problems["checks"][0]["problems"] = [
        {"message": "must not be persisted"}
    ]
    with pytest.raises(ValueError, match="check contains problems"):
        validator.validate_plan(aggregate_problems)

    malformed_resource = _plan(validator)
    malformed_resource["checks"].append(
        {
            "address": {
                "kind": "resource",
                "name": "x",
                "to_display": "garbage",
            },
            "status": "pass",
        }
    )
    with pytest.raises(ValueError, match="resource mode"):
        validator.validate_plan(malformed_resource)


def test_validator_records_passing_resource_preconditions():
    validator = _validator()
    plan = _plan(validator)
    plan["checks"].append(
        {
            "address": {
                "kind": "resource",
                "mode": "managed",
                "name": "github_deploy",
                "to_display": "aws_iam_role_policy.github_deploy",
                "type": "aws_iam_role_policy",
            },
            "status": "pass",
            "instances": [
                {
                    "address": {
                        "to_display": "aws_iam_role_policy.github_deploy"
                    },
                    "status": "pass",
                }
            ],
        }
    )

    summary = validator.validate_plan(plan)

    assert summary["totals"]["checks"] == 3
    resource_check = next(
        row
        for row in summary["checks"]
        if row["to_display"] == "aws_iam_role_policy.github_deploy"
    )
    assert resource_check == {
        "instances": [
            {
                "status": "pass",
                "to_display": "aws_iam_role_policy.github_deploy",
            }
        ],
        "kind": "resource",
        "mode": "managed",
        "name": "github_deploy",
        "status": "pass",
        "to_display": "aws_iam_role_policy.github_deploy",
        "type": "aws_iam_role_policy",
    }


def test_validator_accepts_real_shaped_output_and_zero_count_resource_checks():
    validator = _validator()
    plan = _plan(validator)
    plan["checks"].extend(
        [
            {
                "address": {
                    "kind": "output_value",
                    "name": "deployment_ready",
                    "to_display": "output.deployment_ready",
                },
                "status": "pass",
                "instances": [
                    {
                        "address": {"to_display": "output.deployment_ready"},
                        "status": "pass",
                    }
                ],
            },
            {
                "address": {
                    "kind": "resource",
                    "mode": "managed",
                    "name": "zero",
                    "to_display": "terraform_data.zero",
                    "type": "terraform_data",
                },
                "status": "pass",
            },
        ]
    )

    summary = validator.validate_plan(plan)

    assert summary["totals"]["checks"] == 4
    zero = next(
        row for row in summary["checks"] if row["to_display"] == "terraform_data.zero"
    )
    assert zero["instances"] == []
    output = next(
        row
        for row in summary["checks"]
        if row["to_display"] == "output.deployment_ready"
    )
    assert output["kind"] == "output_value"


def test_validator_rejects_incomplete_error_and_sensitive_plans():
    validator = _validator()
    incomplete = _plan(validator)
    incomplete.pop("configuration")
    with pytest.raises(ValueError, match="incomplete"):
        validator.validate_plan(incomplete)

    no_drift = _plan(validator)
    no_drift.pop("resource_drift")
    assert validator.validate_plan(no_drift)["resource_drift"] == []

    missing_values = _plan(validator)
    missing_values["planned_values"] = None
    with pytest.raises(ValueError, match="planned values must be complete"):
        validator.validate_plan(missing_values)

    errored = _plan(validator)
    errored["complete"] = False
    with pytest.raises(ValueError, match="complete and error-free"):
        validator.validate_plan(errored)

    sensitive = _plan(validator)
    sensitive["resource_changes"][0]["change"]["after"] = {
        "policy": "must-never-appear-in-the-error"
    }
    sensitive["resource_changes"][0]["change"]["after_sensitive"] = {
        "policy": True
    }
    with pytest.raises(ValueError, match="sensitive") as raised:
        validator.validate_plan(sensitive)
    assert "must-never-appear-in-the-error" not in str(raised.value)


def test_validator_allows_only_exact_known_null_provider_placeholders():
    validator = _validator()
    summary = validator.validate_plan(
        _plan_with_null_sensitive_placeholders(validator)
    )

    assert summary["null_sensitive_placeholders"] == [
        {"address": "aws_acm_certificate.demo", "attribute": "private_key"},
        {
            "address": (
                "aws_s3_bucket_object_lock_configuration.learning_evidence[0]"
            ),
            "attribute": "token",
        },
    ]


def test_validator_rejects_non_null_and_unmarked_known_sensitive_values():
    validator = _validator()
    changed = _plan_with_null_sensitive_placeholders(validator)
    _resource_entry(changed, "resource_changes", "aws_acm_certificate.demo")[
        "change"
    ]["after"]["private_key"] = "must-never-appear-in-the-error"
    with pytest.raises(ValueError, match="sensitive") as raised:
        validator.validate_plan(changed)
    assert "must-never-appear-in-the-error" not in str(raised.value)

    prior = _plan_with_null_sensitive_placeholders(validator)
    prior_acm = next(
        entry
        for entry in prior["prior_state"]["values"]["root_module"]["resources"]
        if entry["address"] == "aws_acm_certificate.demo"
    )
    prior_acm["values"]["private_key"] = "must-never-appear-in-the-error"
    prior_acm["sensitive_values"]["private_key"] = False
    with pytest.raises(ValueError, match="sensitive") as raised:
        validator.validate_plan(prior)
    assert "must-never-appear-in-the-error" not in str(raised.value)


@pytest.mark.parametrize(
    "after_unknown",
    [
        True,
        "unknown",
        [],
        {"private_key": True},
        {"private_key": "true"},
    ],
)
def test_validator_rejects_unknown_or_malformed_sensitive_values(after_unknown):
    validator = _validator()
    plan = _plan_with_null_sensitive_placeholders(validator)
    _resource_entry(plan, "resource_changes", "aws_acm_certificate.demo")[
        "change"
    ]["after_unknown"] = after_unknown

    with pytest.raises(ValueError, match="sensitive"):
        validator.validate_plan(plan)


def test_validator_correlates_root_value_and_change_evidence():
    validator = _validator()
    child_module = _plan_with_null_sensitive_placeholders(validator)
    root = child_module["planned_values"]["root_module"]
    acm = root["resources"].pop(0)
    root["child_modules"] = [{"address": "module.unapproved", "resources": [acm]}]
    with pytest.raises(ValueError, match="sensitive"):
        validator.validate_plan(child_module)

    missing = _plan_with_null_sensitive_placeholders(validator)
    missing["prior_state"]["values"]["root_module"]["resources"] = [
        entry
        for entry in missing["prior_state"]["values"]["root_module"]["resources"]
        if entry["address"] != "aws_acm_certificate.demo"
    ]
    with pytest.raises(ValueError, match="sensitive"):
        validator.validate_plan(missing)

    duplicate = _plan_with_null_sensitive_placeholders(validator)
    duplicate["planned_values"]["root_module"]["resources"].append(
        json.loads(
            json.dumps(
                next(
                    entry
                    for entry in duplicate["planned_values"]["root_module"][
                        "resources"
                    ]
                    if entry["address"] == "aws_acm_certificate.demo"
                )
            )
        )
    )
    with pytest.raises(ValueError, match="sensitive"):
        validator.validate_plan(duplicate)

    mismatched = _plan_with_null_sensitive_placeholders(validator)
    change = _resource_entry(
        mismatched, "resource_changes", "aws_acm_certificate.demo"
    )["change"]
    change["after_sensitive"]["private_key"] = False
    change["after_unknown"]["private_key"] = True
    with pytest.raises(ValueError, match="sensitive"):
        validator.validate_plan(mismatched)


def test_validator_rejects_module_deposed_name_and_index_variants():
    validator = _validator()
    variants = [
        ("module_address", "module.unapproved"),
        ("deposed", "deadbeef"),
        ("previous_address", "aws_acm_certificate.previous"),
        ("name", "not_demo"),
        ("index", 0),
    ]
    for field, value in variants:
        plan = _plan_with_null_sensitive_placeholders(validator)
        _resource_entry(plan, "resource_changes", "aws_acm_certificate.demo")[
            field
        ] = value
        with pytest.raises(ValueError, match="sensitive"):
            validator.validate_plan(plan)


def test_validator_rejects_other_known_sensitive_resource_addresses():
    validator = _validator()
    plan = _plan_with_null_sensitive_placeholders(validator)
    plan["planned_values"]["root_module"]["resources"].append(
        _value_resource(
            "aws_acm_certificate.unapproved",
            resource_type="aws_acm_certificate",
            values={"private_key": "must-never-appear-in-the-error"},
            sensitive_values={"private_key": False},
        )
    )

    with pytest.raises(ValueError, match="sensitive") as raised:
        validator.validate_plan(plan)
    assert "must-never-appear-in-the-error" not in str(raised.value)


def test_validator_requires_exact_sensitive_resource_schema_versions():
    validator = _validator()
    for schema_version in (None, "0", 999, True):
        plan = _plan_with_null_sensitive_placeholders(validator)
        acm = next(
            entry
            for entry in plan["planned_values"]["root_module"]["resources"]
            if entry["address"] == "aws_acm_certificate.demo"
        )
        if schema_version is None:
            acm.pop("schema_version")
        else:
            acm["schema_version"] = schema_version
        with pytest.raises(ValueError, match="sensitive"):
            validator.validate_plan(plan)

    change_schema = _plan_with_null_sensitive_placeholders(validator)
    _resource_entry(
        change_schema, "resource_changes", "aws_acm_certificate.demo"
    )["schema_version"] = 0
    with pytest.raises(ValueError, match="sensitive"):
        validator.validate_plan(change_schema)


def test_validator_rejects_malformed_and_unapproved_sensitivity_masks():
    validator = _validator()
    malformed = _plan(validator)
    malformed["resource_changes"][0]["change"]["after"] = {
        "policy": "must-never-appear-in-the-error"
    }
    malformed["resource_changes"][0]["change"]["after_sensitive"] = {
        "policy": 1
    }
    with pytest.raises(ValueError, match="sensitive") as raised:
        validator.validate_plan(malformed)
    assert "must-never-appear-in-the-error" not in str(raised.value)

    output = _plan(validator)
    output["output_changes"]["github_quarantine_redrive_role_arn"][
        "after_sensitive"
    ] = True
    with pytest.raises(ValueError, match="sensitive"):
        validator.validate_plan(output)


@pytest.mark.parametrize(
    ("kind", "entry", "message"),
    [
        (
            "resource_changes",
            _resource("aws_iam_role.removed", ["delete"]),
            "managed resource removals",
        ),
        (
            "resource_changes",
            _resource("aws_iam_role.replaced", ["delete", "create"]),
            "replacements",
        ),
        (
            "resource_drift",
            _resource("aws_iam_role.missing", ["delete"]),
            "drift deletes",
        ),
        (
            "resource_changes",
            _resource(
                "cloudflare_dns_record.changed",
                ["update"],
                resource_type="cloudflare_dns_record",
                provider="registry.terraform.io/cloudflare/cloudflare",
            ),
            "Cloudflare managed changes",
        ),
        (
            "resource_changes",
            _resource(
                "data.aws_s3_bucket.unexpected",
                ["delete"],
                mode="data",
                resource_type="aws_s3_bucket",
            ),
            "unexpected data resource removal",
        ),
    ],
)
def test_validator_rejects_destructive_or_unapproved_resource_actions(
    kind, entry, message
):
    validator = _validator()
    plan = _plan(validator)
    plan[kind] = [entry]

    with pytest.raises(ValueError, match=message):
        validator.validate_plan(plan)


def test_validator_rejects_unapproved_output_removal():
    validator = _validator()
    plan = _plan(validator)
    plan["output_changes"] = {
        "unexpected": {
            "actions": ["delete"],
            "before": "value",
            "after": None,
        }
    }

    with pytest.raises(ValueError, match="unexpected output removal"):
        validator.validate_plan(plan)


@pytest.mark.parametrize(
    "output_name",
    [
        "learning_corpus_kms_key_alias",
        "learning_corpus_kms_key_arn",
        "tenant_lifecycle_export_bucket_arn",
    ],
)
def test_validator_rejects_retired_output_removals_outside_the_exact_rollout(
    output_name,
):
    validator = _validator()
    plan = _plan(validator)
    plan["output_changes"] = {
        output_name: {
            "actions": ["delete"],
            "before": "retired",
            "after": None,
            "before_sensitive": False,
            "after_sensitive": False,
        }
    }

    with pytest.raises(ValueError, match="unapproved desired-state output mutation"):
        validator.validate_plan(plan)


def test_validator_rejects_the_previously_allowed_data_resource_removal():
    validator = _validator()
    plan = _no_change_plan(validator)
    plan["resource_changes"].append(
        _resource(
            "data.aws_s3_bucket.state",
            ["delete"],
            mode="data",
            resource_type="aws_s3_bucket",
        )
    )

    with pytest.raises(ValueError, match="unapproved desired-state resource mutation"):
        validator.validate_plan(plan)


def test_state_provenance_retains_only_lineage_serial_and_terraform_version():
    validator = _validator()
    state = {
        **_provenance(),
        "version": 4,
        "outputs": {"secret": {"value": "must-not-persist"}},
        "resources": [{"instances": [{"attributes": {"password": "hidden"}}]}],
    }

    assert validator.state_provenance(state) == _provenance()

    with pytest.raises(ValueError, match="lineage"):
        validator.state_provenance({**state, "lineage": "not-a-uuid"})
    with pytest.raises(ValueError, match="serial"):
        validator.state_provenance({**state, "serial": -1})


def test_manifest_binds_exact_files_metadata_actions_and_unchanged_state(tmp_path: Path):
    validator = _validator()
    paths = {
        key: tmp_path / name for key, name in validator.EXPECTED_ARTIFACT_NAMES.items()
    }
    paths["plan"].write_bytes(b"saved terraform plan")
    paths["plan_json"].write_text(json.dumps(_plan(validator)), encoding="utf-8")
    paths["lock"].write_text("provider lock", encoding="utf-8")
    paths["state_before"].write_text(json.dumps(_provenance()), encoding="utf-8")
    paths["state_after"].write_text(json.dumps(_provenance()), encoding="utf-8")

    manifest = validator.build_manifest(
        plan_json=paths["plan_json"],
        plan_file=paths["plan"],
        lock_file=paths["lock"],
        state_before_file=paths["state_before"],
        state_after_file=paths["state_after"],
        actions_file=paths["actions"],
        manifest_file=paths["manifest"],
        source_revision=SOURCE_REVISION,
        repository=validator.EXPECTED_REPOSITORY,
        workflow_ref=WORKFLOW_REF,
        aws_account_id="762397612117",
        environment="demo",
        role_arn="arn:aws:iam::762397612117:role/hindsight-github-bootstrap-plan",
        run_id="1234",
        run_attempt="2",
    )

    assert manifest["source_revision"] == SOURCE_REVISION
    assert manifest["workflow_ref"] == WORKFLOW_REF
    assert manifest["run_id"] == 1234
    assert manifest["run_attempt"] == 2
    assert manifest["aws"] == {
        "account_id": "762397612117",
        "region": "us-east-1",
        "role_arn": "arn:aws:iam::762397612117:role/hindsight-github-bootstrap-plan",
    }
    assert manifest["environment"] == "demo"
    assert manifest["plan"]["null_sensitive_placeholders"] == []
    assert manifest["plan"]["reconciled_refresh_drift"] == []
    assert manifest["state_provenance"] == {
        "before": _provenance(),
        "after": _provenance(),
    }
    assert set(manifest["artifacts"]) == {
        "bootstrap.tfplan",
        "bootstrap.tfplan.json",
        "bootstrap-plan-actions.json",
        "bootstrap.terraform.lock.hcl",
        "bootstrap-state-before.json",
        "bootstrap-state-after.json",
    }
    assert json.loads(paths["manifest"].read_text()) == manifest
    assert json.loads(paths["actions"].read_text())["totals"] == {
        "checks": 2,
        "resource_changes": 9,
        "resource_drift": 0,
        "output_changes": 2,
    }

    reconciled_plan = _plan_with_cloudflare_refresh_drift(validator)
    paths["plan_json"].write_text(json.dumps(reconciled_plan), encoding="utf-8")
    reconciled_manifest = validator.build_manifest(
        plan_json=paths["plan_json"],
        plan_file=paths["plan"],
        lock_file=paths["lock"],
        state_before_file=paths["state_before"],
        state_after_file=paths["state_after"],
        actions_file=paths["actions"],
        manifest_file=paths["manifest"],
        source_revision=SOURCE_REVISION,
        repository=validator.EXPECTED_REPOSITORY,
        workflow_ref=WORKFLOW_REF,
        aws_account_id="762397612117",
        environment="demo",
        role_arn="arn:aws:iam::762397612117:role/hindsight-github-bootstrap-plan",
        run_id="1234",
        run_attempt="2",
    )
    address = next(iter(validator.ALLOWED_CLOUDFLARE_REFRESH_DRIFT))
    assert reconciled_manifest["plan"]["reconciled_refresh_drift"] == [address]
    assert json.loads(paths["actions"].read_text())[
        "reconciled_refresh_drift"
    ] == [address]

    paths["state_after"].write_text(json.dumps(_provenance(serial=26)), encoding="utf-8")
    with pytest.raises(ValueError, match="state changed"):
        validator.build_manifest(
            plan_json=paths["plan_json"],
            plan_file=paths["plan"],
            lock_file=paths["lock"],
            state_before_file=paths["state_before"],
            state_after_file=paths["state_after"],
            actions_file=paths["actions"],
            manifest_file=paths["manifest"],
            source_revision=SOURCE_REVISION,
            repository=validator.EXPECTED_REPOSITORY,
            workflow_ref=WORKFLOW_REF,
            aws_account_id="762397612117",
            environment="demo",
            role_arn="arn:aws:iam::762397612117:role/hindsight-github-bootstrap-plan",
            run_id="1234",
            run_attempt="2",
        )


def test_manifest_records_only_correlated_null_placeholders_and_fails_before_writes(
    tmp_path: Path,
):
    validator = _validator()

    def inputs(directory: Path, plan: dict):
        directory.mkdir()
        paths = {
            key: directory / name
            for key, name in validator.EXPECTED_ARTIFACT_NAMES.items()
        }
        paths["plan"].write_bytes(b"saved terraform plan")
        paths["plan_json"].write_text(json.dumps(plan), encoding="utf-8")
        paths["lock"].write_text("provider lock", encoding="utf-8")
        paths["state_before"].write_text(
            json.dumps(_provenance()), encoding="utf-8"
        )
        paths["state_after"].write_text(
            json.dumps(_provenance()), encoding="utf-8"
        )
        return paths

    expected = [
        {"address": "aws_acm_certificate.demo", "attribute": "private_key"},
        {
            "address": (
                "aws_s3_bucket_object_lock_configuration.learning_evidence[0]"
            ),
            "attribute": "token",
        },
    ]
    accepted = inputs(
        tmp_path / "accepted",
        _plan_with_null_sensitive_placeholders(validator),
    )
    manifest = validator.build_manifest(
        plan_json=accepted["plan_json"],
        plan_file=accepted["plan"],
        lock_file=accepted["lock"],
        state_before_file=accepted["state_before"],
        state_after_file=accepted["state_after"],
        actions_file=accepted["actions"],
        manifest_file=accepted["manifest"],
        source_revision=SOURCE_REVISION,
        repository=validator.EXPECTED_REPOSITORY,
        workflow_ref=WORKFLOW_REF,
        aws_account_id="762397612117",
        environment="demo",
        role_arn="arn:aws:iam::762397612117:role/hindsight-github-bootstrap-plan",
        run_id="1234",
        run_attempt="2",
    )
    assert manifest["plan"]["null_sensitive_placeholders"] == expected
    assert json.loads(accepted["actions"].read_text())[
        "null_sensitive_placeholders"
    ] == expected

    blocked_plan = _plan_with_null_sensitive_placeholders(validator)
    _resource_entry(
        blocked_plan, "resource_changes", "aws_acm_certificate.demo"
    )["change"]["after"]["private_key"] = "must-never-be-written"
    blocked = inputs(tmp_path / "blocked", blocked_plan)
    with pytest.raises(ValueError, match="sensitive") as raised:
        validator.build_manifest(
            plan_json=blocked["plan_json"],
            plan_file=blocked["plan"],
            lock_file=blocked["lock"],
            state_before_file=blocked["state_before"],
            state_after_file=blocked["state_after"],
            actions_file=blocked["actions"],
            manifest_file=blocked["manifest"],
            source_revision=SOURCE_REVISION,
            repository=validator.EXPECTED_REPOSITORY,
            workflow_ref=WORKFLOW_REF,
            aws_account_id="762397612117",
            environment="demo",
            role_arn=(
                "arn:aws:iam::762397612117:role/hindsight-github-bootstrap-plan"
            ),
            run_id="1234",
            run_attempt="2",
        )
    assert "must-never-be-written" not in str(raised.value)
    assert not blocked["actions"].exists()
    assert not blocked["manifest"].exists()


def test_manifest_refuses_a_different_well_formed_aws_account(tmp_path: Path):
    validator = _validator()
    paths = {
        key: tmp_path / name for key, name in validator.EXPECTED_ARTIFACT_NAMES.items()
    }
    paths["plan"].write_bytes(b"saved terraform plan")
    paths["plan_json"].write_text(json.dumps(_plan(validator)), encoding="utf-8")
    paths["lock"].write_text("provider lock", encoding="utf-8")
    paths["state_before"].write_text(json.dumps(_provenance()), encoding="utf-8")
    paths["state_after"].write_text(json.dumps(_provenance()), encoding="utf-8")

    with pytest.raises(ValueError, match="account identity is not authorized"):
        validator.build_manifest(
            plan_json=paths["plan_json"],
            plan_file=paths["plan"],
            lock_file=paths["lock"],
            state_before_file=paths["state_before"],
            state_after_file=paths["state_after"],
            actions_file=paths["actions"],
            manifest_file=paths["manifest"],
            source_revision=SOURCE_REVISION,
            repository=validator.EXPECTED_REPOSITORY,
            workflow_ref=WORKFLOW_REF,
            aws_account_id="123456789012",
            environment="demo",
            role_arn="arn:aws:iam::123456789012:role/hindsight-github-bootstrap-plan",
            run_id="1234",
            run_attempt="2",
        )


def test_preapply_revalidates_all_saved_artifacts_without_rewriting_evidence(
    tmp_path: Path,
):
    validator = _validator()
    paths, _ = _receipt_inputs(tmp_path / "valid", validator)
    original_manifest = paths["manifest"].read_bytes()
    original_actions = paths["actions"].read_bytes()

    result = validator.validate_preapply(**_preapply_kwargs(paths))

    assert result is None
    assert paths["manifest"].read_bytes() == original_manifest
    assert paths["actions"].read_bytes() == original_actions


@pytest.mark.parametrize(
    "artifact",
    ["plan", "plan_json", "actions", "lock", "state_before", "state_after"],
)
def test_preapply_rejects_any_saved_artifact_changed_after_manifest_creation(
    tmp_path: Path, artifact: str
):
    validator = _validator()
    paths, _ = _receipt_inputs(tmp_path / artifact, validator)
    paths[artifact].write_bytes(paths[artifact].read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="no longer match their manifest"):
        validator.validate_preapply(**_preapply_kwargs(paths))


def test_preapply_requires_the_manifest_exact_artifact_digest_map(tmp_path: Path):
    validator = _validator()
    paths, _ = _receipt_inputs(tmp_path / "missing", validator)
    manifest = json.loads(paths["manifest"].read_text())
    manifest["artifacts"].pop("bootstrap.tfplan")
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact set is incomplete"):
        validator.validate_preapply(**_preapply_kwargs(paths))

    paths, _ = _receipt_inputs(tmp_path / "extra", validator)
    manifest = json.loads(paths["manifest"].read_text())
    manifest["artifacts"]["unapproved.json"] = "0" * 64
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact set is incomplete"):
        validator.validate_preapply(**_preapply_kwargs(paths))


def test_preapply_requires_current_state_to_equal_the_planned_provenance(
    tmp_path: Path,
):
    validator = _validator()
    paths, _ = _receipt_inputs(tmp_path / "stale", validator)
    paths["state_before_apply"].write_text(
        json.dumps(_provenance(serial=26)), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="does not match the saved plan"):
        validator.validate_preapply(**_preapply_kwargs(paths))


def test_preapply_requires_initial_exit_code_to_match_sanitized_actions(
    tmp_path: Path,
):
    validator = _validator()
    paths, _ = _receipt_inputs(tmp_path / "mutations", validator)
    with pytest.raises(ValueError, match="exit code is inconsistent"):
        validator.validate_preapply(
            **_preapply_kwargs(paths, plan_exit_code="0")
        )
    with pytest.raises(ValueError, match="detailed exit code"):
        validator.validate_preapply(
            **_preapply_kwargs(paths, plan_exit_code="1")
        )

    plan = _plan(validator)
    for entry in plan["resource_changes"]:
        entry["change"]["actions"] = ["no-op"]
    for change in plan["output_changes"].values():
        change["actions"] = ["no-op"]
    plan["applyable"] = False
    noop_paths, _ = _receipt_inputs(
        tmp_path / "noop", validator, plan=plan, after_serial=25
    )

    assert (
        validator.validate_preapply(
            **_preapply_kwargs(noop_paths, plan_exit_code="0")
        )
        is None
    )


def test_receipt_binds_apply_metadata_actions_and_state_without_raw_values(
    tmp_path: Path,
):
    validator = _validator()
    paths, kwargs = _receipt_inputs(tmp_path / "applied", validator)

    receipt = validator.build_receipt(**kwargs)

    assert receipt["schema_version"] == validator.RECEIPT_SCHEMA_VERSION
    assert receipt["source_revision"] == SOURCE_REVISION
    assert receipt["repository"] == validator.EXPECTED_REPOSITORY
    assert receipt["workflow_ref"] == WORKFLOW_REF
    assert receipt["run_id"] == 1234
    assert receipt["run_attempt"] == 2
    assert receipt["environment"] == "demo"
    assert receipt["aws"] == {
        "account_id": "762397612117",
        "apply_role_arn": (
            "arn:aws:iam::762397612117:role/hindsight-github-bootstrap-apply"
        ),
        "plan_role_arn": (
            "arn:aws:iam::762397612117:role/hindsight-github-bootstrap-plan"
        ),
        "region": "us-east-1",
    }
    assert receipt["plan"] == {
        "action_counts": {"create": 8, "no-op": 1, "update": 2},
        "applied": True,
        "applyable": True,
        "detailed_exit_code": 2,
        "mutation_action_counts": {"create": 8, "update": 2},
        "totals": {
            "checks": 2,
            "output_changes": 2,
            "resource_changes": 9,
            "resource_drift": 0,
        },
    }
    assert receipt["postcheck"] == {
        "detailed_exit_code": 0,
        "succeeded": True,
    }
    assert receipt["state_provenance"] == {
        "plan": _provenance(),
        "before_apply": _provenance(),
        "after_apply": _provenance(serial=26),
        "after_postcheck": _provenance(serial=26),
    }
    assert set(receipt["artifacts"]) == {
        "bootstrap-plan-actions.json",
        "bootstrap-plan-manifest.json",
    }
    assert all(
        len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        for digest in receipt["artifacts"].values()
    )
    assert json.loads(paths["receipt"].read_text()) == receipt
    serialized = json.dumps(receipt)
    for raw_plan_detail in (
        "aws_iam_role.github_worker_acceptance",
        "cloudflare_dns_record.acm_validation",
        "github_worker_acceptance_role_arn",
    ):
        assert raw_plan_detail not in serialized


def test_receipt_accepts_exit_zero_only_for_a_non_applyable_noop_plan(
    tmp_path: Path,
):
    validator = _validator()
    plan = _plan(validator)
    for entry in plan["resource_changes"]:
        entry["change"]["actions"] = ["no-op"]
    for change in plan["output_changes"].values():
        change["actions"] = ["no-op"]
    plan["applyable"] = False
    paths, kwargs = _receipt_inputs(
        tmp_path / "noop", validator, plan=plan, after_serial=25
    )
    kwargs["plan_exit_code"] = "0"

    receipt = validator.build_receipt(**kwargs)

    assert receipt["plan"]["applied"] is False
    assert receipt["plan"]["applyable"] is False
    assert receipt["plan"]["detailed_exit_code"] == 0
    assert receipt["plan"]["mutation_action_counts"] == {}
    assert receipt["plan"]["action_counts"] == {"no-op": 11}
    assert receipt["state_provenance"]["before_apply"] == _provenance()
    assert receipt["state_provenance"]["after_apply"] == _provenance()
    assert json.loads(paths["receipt"].read_text()) == receipt


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source_revision", "A" * 40, "source revision"),
        ("repository", "different/repository", "repository identity"),
        (
            "workflow_ref",
            "OCHOLA-EDDYPHIL/hindsight/.github/workflows/other.yml@refs/heads/main",
            "workflow identity",
        ),
        ("aws_account_id", "123456789012", "account identity"),
        ("environment", "production", "demo environment"),
        (
            "plan_role_arn",
            "arn:aws:iam::762397612117:role/different-plan-role",
            "plan role identity",
        ),
        (
            "apply_role_arn",
            "arn:aws:iam::762397612117:role/different-apply-role",
            "apply role identity",
        ),
        ("run_id", "0", "positive decimal"),
        ("run_attempt", "01", "positive decimal"),
    ],
)
def test_receipt_rejects_any_execution_identity_variant(
    tmp_path: Path, field: str, value: str, message: str
):
    validator = _validator()
    _, kwargs = _receipt_inputs(tmp_path / field, validator)
    kwargs[field] = value

    with pytest.raises(ValueError, match=message):
        validator.build_receipt(**kwargs)


def test_receipt_rejects_tampered_actions_or_manifest_binding(tmp_path: Path):
    validator = _validator()
    paths, kwargs = _receipt_inputs(tmp_path / "tampered-actions", validator)
    actions = json.loads(paths["actions"].read_text())
    actions["action_counts"]["update"] += 1
    paths["actions"].write_text(json.dumps(actions), encoding="utf-8")

    with pytest.raises(ValueError, match="actions digest"):
        validator.build_receipt(**kwargs)
    assert not paths["receipt"].exists()

    paths, kwargs = _receipt_inputs(tmp_path / "tampered-manifest", validator)
    manifest = json.loads(paths["manifest"].read_text())
    manifest["source_revision"] = "b" * 40
    paths["manifest"].write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="execution identity"):
        validator.build_receipt(**kwargs)
    assert not paths["receipt"].exists()


def test_receipt_rejects_exit_codes_inconsistent_with_applyability_and_mutations(
    tmp_path: Path,
):
    validator = _validator()
    _, applied_kwargs = _receipt_inputs(tmp_path / "applied", validator)
    applied_kwargs["plan_exit_code"] = "0"
    with pytest.raises(ValueError, match="exit code is inconsistent"):
        validator.build_receipt(**applied_kwargs)

    plan = _plan(validator)
    for entry in plan["resource_changes"]:
        entry["change"]["actions"] = ["no-op"]
    for change in plan["output_changes"].values():
        change["actions"] = ["no-op"]
    plan["applyable"] = False
    _, noop_kwargs = _receipt_inputs(
        tmp_path / "noop", validator, plan=plan, after_serial=25
    )
    with pytest.raises(ValueError, match="exit code is inconsistent"):
        validator.build_receipt(**noop_kwargs)

    _, invalid_kwargs = _receipt_inputs(tmp_path / "invalid", validator)
    invalid_kwargs["plan_exit_code"] = "1"
    with pytest.raises(ValueError, match="detailed exit code"):
        validator.build_receipt(**invalid_kwargs)


def test_receipt_rejects_an_applyable_flag_without_desired_state_mutations(
    tmp_path: Path,
):
    validator = _validator()
    plan = _plan(validator)
    for entry in plan["resource_changes"]:
        entry["change"]["actions"] = ["no-op"]
    for change in plan["output_changes"].values():
        change["actions"] = ["no-op"]
    plan["applyable"] = True
    _, kwargs = _receipt_inputs(tmp_path / "inconsistent", validator, plan=plan)

    with pytest.raises(ValueError, match="applyable status"):
        validator.build_receipt(**kwargs)


def test_receipt_rejects_state_before_apply_that_is_not_the_planned_state(
    tmp_path: Path,
):
    validator = _validator()
    paths, kwargs = _receipt_inputs(tmp_path / "stale", validator)
    paths["state_before_apply"].write_text(
        json.dumps(_provenance(serial=24)), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="does not match the saved plan"):
        validator.build_receipt(**kwargs)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lineage", "8372fb11-fcec-4fb0-84fa-0ab76fed9e2b"),
        ("terraform_version", "1.15.9"),
    ],
)
def test_receipt_rejects_state_lineage_or_version_changes(
    tmp_path: Path, field: str, value: str
):
    validator = _validator()
    paths, kwargs = _receipt_inputs(tmp_path / field, validator)
    after = _provenance(serial=26)
    after[field] = value
    paths["state_after_apply"].write_text(json.dumps(after), encoding="utf-8")

    with pytest.raises(ValueError, match="lineage or Terraform version"):
        validator.build_receipt(**kwargs)


def test_receipt_requires_serial_transition_matching_whether_apply_ran(
    tmp_path: Path,
):
    validator = _validator()
    _, applied_kwargs = _receipt_inputs(
        tmp_path / "applied", validator, after_serial=25
    )
    with pytest.raises(ValueError, match="serial did not increase"):
        validator.build_receipt(**applied_kwargs)

    plan = _plan(validator)
    for entry in plan["resource_changes"]:
        entry["change"]["actions"] = ["no-op"]
    for change in plan["output_changes"].values():
        change["actions"] = ["no-op"]
    plan["applyable"] = False
    _, noop_kwargs = _receipt_inputs(
        tmp_path / "noop", validator, plan=plan, after_serial=26
    )
    noop_kwargs["plan_exit_code"] = "0"
    with pytest.raises(ValueError, match="no-op plan changed"):
        validator.build_receipt(**noop_kwargs)


def test_receipt_requires_postcheck_zero_and_unchanged_state(tmp_path: Path):
    validator = _validator()
    _, exit_kwargs = _receipt_inputs(tmp_path / "exit", validator)
    exit_kwargs["postcheck_exit_code"] = "2"
    with pytest.raises(ValueError, match="post-apply check"):
        validator.build_receipt(**exit_kwargs)

    paths, state_kwargs = _receipt_inputs(tmp_path / "state", validator)
    paths["state_after_postcheck"].write_text(
        json.dumps(_provenance(serial=27)), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="state changed during"):
        validator.build_receipt(**state_kwargs)


def test_bootstrap_plan_workflow_is_publicly_gated_and_applies_the_bound_plan():
    workflow_path = ROOT / ".github/workflows/plan-bootstrap.yml"
    text = workflow_path.read_text()
    workflow = yaml.safe_load(text)
    jobs = workflow["jobs"]
    authorize = jobs["authorize"]
    apply = jobs["apply"]

    assert set(jobs) == {"authorize", "apply"}
    assert workflow["permissions"] == {}
    triggers = workflow.get("on", workflow.get(True))
    confirmation = triggers["workflow_dispatch"]["inputs"]["confirmation"]
    assert confirmation["required"] is True
    assert confirmation["type"] == "string"
    assert workflow["concurrency"] == {
        "group": "hindsight-bootstrap-apply-demo",
        "cancel-in-progress": False,
    }
    assert authorize["runs-on"] == "${{ vars.HINDSIGHT_RUNNER_LABEL }}"
    assert authorize["permissions"] == {}
    authorization = authorize["steps"][0]["run"]
    for required in (
        '"$EVENT_NAME" == "workflow_dispatch"',
        '"$REPOSITORY" == "OCHOLA-EDDYPHIL/hindsight"',
        '"$REPOSITORY_OWNER" == "OCHOLA-EDDYPHIL"',
        '"$PRIVATE_REPOSITORY" == "false"',
        '"$REPOSITORY_VISIBILITY" == "public"',
        '"$REF_NAME" == "refs/heads/main"',
        '"$REF_PROTECTED" == "true"',
        '"$CONFIRMATION" == "apply-bootstrap-$EVENT_SHA"',
        (
            '"$WORKFLOW_REF" == '
            '"$REPOSITORY/.github/workflows/plan-bootstrap.yml@$REF_NAME"'
        ),
        '"$ACTOR" == "$REPOSITORY_OWNER"',
        '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"',
    ):
        assert required in authorization

    assert apply["runs-on"] == "${{ vars.HINDSIGHT_RUNNER_LABEL }}"
    assert apply["environment"] == "demo"
    assert apply["permissions"] == {"contents": "read", "id-token": "write"}
    assert apply["env"]["AWS_REGION"] == "us-east-1"
    assert apply["env"]["EXPECTED_AWS_ACCOUNT_ID"] == "762397612117"
    assert apply["env"]["CONFIGURED_AWS_ACCOUNT_ID"] == "${{ vars.AWS_ACCOUNT_ID }}"
    assert apply["env"]["TF_VAR_expected_aws_account_id"] == "762397612117"
    assert apply["env"]["TF_VAR_existing_github_oidc_provider_arn"] == (
        "arn:aws:iam::762397612117:oidc-provider/token.actions.githubusercontent.com"
    )
    assert apply["env"]["TF_STATE_KEY"] == "hindsight/bootstrap/terraform.tfstate"
    assert apply["env"]["PLAN_ROLE_ARN"] == "${{ vars.AWS_BOOTSTRAP_PLAN_ROLE_ARN }}"
    assert apply["env"]["APPLY_ROLE_ARN"] == "${{ vars.AWS_BOOTSTRAP_APPLY_ROLE_ARN }}"
    assert "CLOUDFLARE_API_TOKEN" not in apply["env"]

    checkout = next(step for step in apply["steps"] if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"] == {
        "ref": "${{ needs.authorize.outputs.source_revision }}",
        "persist-credentials": False,
    }
    terraform = next(
        step for step in apply["steps"] if step.get("uses") == "hashicorp/setup-terraform@v3"
    )
    assert terraform["with"] == {
        "terraform_version": "1.13.5",
        "terraform_wrapper": False,
    }
    credential_steps = [
        step
        for step in apply["steps"]
        if step.get("uses") == "aws-actions/configure-aws-credentials@v4"
    ]
    assert [step["name"] for step in credential_steps] == [
        "Assume bootstrap plan role",
        "Assume lifecycle-owned bootstrap apply role",
        "Reassume bootstrap plan role",
    ]
    assert [step["with"]["role-to-assume"] for step in credential_steps] == [
        "${{ env.PLAN_ROLE_ARN }}",
        "${{ env.APPLY_ROLE_ARN }}",
        "${{ env.PLAN_ROLE_ARN }}",
    ]
    initial_credentials, chained_credentials, reassumed_credentials = credential_steps
    assert initial_credentials["with"]["unset-current-credentials"] is True
    assert reassumed_credentials["with"]["unset-current-credentials"] is True
    assert "unset-current-credentials" not in chained_credentials["with"]
    assert chained_credentials["with"]["role-chaining"] is True
    assert chained_credentials["with"]["role-external-id"] == (
        "${{ secrets.AWS_BOOTSTRAP_APPLY_EXTERNAL_ID }}"
    )
    assert chained_credentials["with"]["role-skip-session-tagging"] is True
    assert all(
        "role-chaining" not in step["with"]
        and "role-external-id" not in step["with"]
        for step in (initial_credentials, reassumed_credentials)
    )
    assert text.count("AWS_BOOTSTRAP_APPLY_EXTERNAL_ID") == 1
    assert "AWS_BOOTSTRAP_APPLY_EXTERNAL_ID" not in apply["env"]
    assert all(
        "AWS_BOOTSTRAP_APPLY_EXTERNAL_ID" not in step.get("env", {})
        and "AWS_BOOTSTRAP_APPLY_EXTERNAL_ID" not in step.get("run", "")
        for step in apply["steps"]
    )
    assert chained_credentials["if"] == "steps.bootstrap_plan.outputs.exit_code == '2'"
    protected_configuration = next(
        step
        for step in apply["steps"]
        if step.get("name") == "Verify protected apply configuration"
    )["run"]
    assert 'test "$CONFIGURED_AWS_ACCOUNT_ID" = "$EXPECTED_AWS_ACCOUNT_ID"' in (
        protected_configuration
    )
    assert (
        'test "$PLAN_ROLE_ARN" = '
        '"arn:aws:iam::${EXPECTED_AWS_ACCOUNT_ID}:role/hindsight-github-bootstrap-plan"'
    ) in protected_configuration
    assert (
        'test "$APPLY_ROLE_ARN" = '
        '"arn:aws:iam::${EXPECTED_AWS_ACCOUNT_ID}:role/hindsight-github-bootstrap-apply"'
    ) in protected_configuration

    plan_step = next(
        step for step in apply["steps"] if step.get("name") == "Create full refreshed locked bootstrap plan"
    )
    assert plan_step["env"] == {
        "CLOUDFLARE_API_TOKEN": "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    }
    assert plan_step["id"] == "bootstrap_plan"
    for flag in (
        "-input=false",
        "-lock=true",
        "-lock-timeout=5m",
        "-refresh=true",
        "-detailed-exitcode",
    ):
        assert flag in plan_step["run"]
    assert '-out="$PLAN_DIR/bootstrap.tfplan"' in plan_step["run"]
    assert "printf 'exit_code=%s\\n'" in plan_step["run"]
    for forbidden in ("-target", "-destroy", "-refresh=false"):
        assert forbidden not in text

    step_names = [step.get("name") for step in apply["steps"]]
    ordered_names = [
        "Create full refreshed locked bootstrap plan",
        "Validate and bind complete plan actions",
        "Record state provenance before apply",
        "Revalidate exact saved plan before apply",
        "Assume lifecycle-owned bootstrap apply role",
        "Verify bootstrap apply identity",
        "Apply exact saved bootstrap plan",
        "Reassume bootstrap plan role",
        "Verify bootstrap postcheck identity",
        "Record state provenance after apply",
        "Require clean full refreshed bootstrap postcheck",
        "Record state provenance after postcheck",
        "Create sanitized bootstrap apply receipt",
        "Upload sanitized bootstrap apply evidence",
        "Remove exact temporary workspace",
    ]
    assert [step_names.index(name) for name in ordered_names] == sorted(
        step_names.index(name) for name in ordered_names
    )

    validate = next(
        step
        for step in apply["steps"]
        if step.get("name") == "Validate and bind complete plan actions"
    )
    for path_argument in (
        '--plan-file "$PLAN_DIR/bootstrap.tfplan"',
        '--actions-output "$PLAN_DIR/bootstrap-plan-actions.json"',
        '--manifest-output "$PLAN_DIR/bootstrap-plan-manifest.json"',
    ):
        assert path_argument in validate["run"]

    preapply = next(
        step
        for step in apply["steps"]
        if step.get("name") == "Revalidate exact saved plan before apply"
    )
    assert preapply["env"] == {
        "PLAN_EXIT_CODE": "${{ steps.bootstrap_plan.outputs.exit_code }}"
    }
    for argument in (
        "validate_bootstrap_plan.py preapply",
        '--manifest "$PLAN_DIR/bootstrap-plan-manifest.json"',
        '--actions "$PLAN_DIR/bootstrap-plan-actions.json"',
        '--plan-file "$PLAN_DIR/bootstrap.tfplan"',
        '--plan-json "$PLAN_DIR/bootstrap.tfplan.json"',
        '--lock-file "$PLAN_DIR/bootstrap.terraform.lock.hcl"',
        '--state-before-plan "$PLAN_DIR/bootstrap-state-before.json"',
        '--state-after-plan "$PLAN_DIR/bootstrap-state-after.json"',
        '--state-before-apply "$PLAN_DIR/bootstrap-state-before-apply.json"',
        '--plan-exit-code "$PLAN_EXIT_CODE"',
    ):
        assert argument in preapply["run"]

    apply_step = next(
        step
        for step in apply["steps"]
        if step.get("name") == "Apply exact saved bootstrap plan"
    )
    assert apply_step["if"] == "steps.bootstrap_plan.outputs.exit_code == '2'"
    assert apply_step["env"] == {
        "CLOUDFLARE_API_TOKEN": "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    }
    assert "terraform -chdir=infra/terraform/bootstrap apply" in apply_step["run"]
    assert '"$PLAN_DIR/bootstrap.tfplan"' in apply_step["run"]

    postcheck = next(
        step
        for step in apply["steps"]
        if step.get("name") == "Require clean full refreshed bootstrap postcheck"
    )
    assert postcheck["id"] == "postcheck"
    assert postcheck["env"] == {
        "CLOUDFLARE_API_TOKEN": "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    }
    for flag in ("-refresh=true", "-detailed-exitcode", "-lock=true"):
        assert flag in postcheck["run"]
    assert 'test "$POSTCHECK_EXIT_CODE" -eq 0' in postcheck["run"]

    receipt = next(
        step
        for step in apply["steps"]
        if step.get("name") == "Create sanitized bootstrap apply receipt"
    )
    assert receipt["env"]["PLAN_EXIT_CODE"] == (
        "${{ steps.bootstrap_plan.outputs.exit_code }}"
    )
    assert receipt["env"]["POSTCHECK_EXIT_CODE"] == (
        "${{ steps.postcheck.outputs.exit_code }}"
    )
    for argument in (
        "validate_bootstrap_plan.py receipt",
        '--manifest "$PLAN_DIR/bootstrap-plan-manifest.json"',
        '--actions "$PLAN_DIR/bootstrap-plan-actions.json"',
        '--state-before-apply "$PLAN_DIR/bootstrap-state-before-apply.json"',
        '--state-after-apply "$PLAN_DIR/bootstrap-state-after-apply.json"',
        '--state-after-postcheck "$PLAN_DIR/bootstrap-state-after-postcheck.json"',
        '--output "$PLAN_DIR/bootstrap-apply-receipt.json"',
        '--plan-exit-code "$PLAN_EXIT_CODE"',
        '--postcheck-exit-code "$POSTCHECK_EXIT_CODE"',
    ):
        assert argument in receipt["run"]

    token_steps = [
        step
        for step in apply["steps"]
        if "CLOUDFLARE_API_TOKEN" in step.get("env", {})
    ]
    assert [step["name"] for step in token_steps] == [
        "Create full refreshed locked bootstrap plan",
        "Apply exact saved bootstrap plan",
        "Require clean full refreshed bootstrap postcheck",
    ]
    assert text.count("secrets.CLOUDFLARE_API_TOKEN") == 3
    assert all(
        "CLOUDFLARE_API_TOKEN" not in step.get("run", "")
        for step in apply["steps"]
    )

    assert "-target" not in text
    assert "-destroy" not in text
    assert "-refresh=false" not in text
    assert "PLAN_DIR" not in apply["env"]
    assert "TF_DATA_DIR" not in apply["env"]
    assert "TF_PLUGIN_CACHE_DIR" not in apply["env"]
    assert all("runner.temp" not in str(value) for value in apply["env"].values())
    prepare = next(
        step for step in apply["steps"] if step.get("name") == "Prepare exact temporary workspace"
    )
    assert 'PLAN_DIR="$RUNNER_TEMP/hindsight-bootstrap-apply-' in prepare["run"]
    assert 'test "$TF_DATA_DIR" = "$PLAN_DIR/terraform-data"' in prepare["run"]
    assert 'mkdir -m 0700 -- "$PLAN_DIR/terraform-data"' in prepare["run"]
    assert 'printf \'PLAN_DIR=%s\\n\'' in prepare["run"]
    assert '>> "$GITHUB_ENV"' in prepare["run"]

    provenance_steps = [
        step
        for step in apply["steps"]
        if step.get("name", "").startswith("Record state provenance")
    ]
    assert len(provenance_steps) == 5
    for step in provenance_steps:
        assert 'aws s3 cp "s3://$TF_STATE_BUCKET/$TF_STATE_KEY" -' in step["run"]
        assert "state-provenance" in step["run"]

    upload = next(
        step for step in apply["steps"] if step.get("uses") == "actions/upload-artifact@v4"
    )
    assert upload["with"]["retention-days"] == 14
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["path"].splitlines() == [
        "${{ env.PLAN_DIR }}/bootstrap-plan-actions.json",
        "${{ env.PLAN_DIR }}/bootstrap-plan-manifest.json",
        "${{ env.PLAN_DIR }}/bootstrap-apply-receipt.json",
    ]
    for raw_artifact in (
        "bootstrap.tfplan",
        "bootstrap.tfplan.json",
        "bootstrap.terraform.lock.hcl",
        "bootstrap-state-before.json",
        "bootstrap-state-after.json",
        "bootstrap-state-before-apply.json",
        "bootstrap-state-after-apply.json",
        "bootstrap-state-after-postcheck.json",
        "bootstrap-postcheck.tfplan",
    ):
        assert raw_artifact not in upload["with"]["path"]

    cleanup = next(
        step for step in apply["steps"] if step.get("name") == "Remove exact temporary workspace"
    )
    assert cleanup["if"] == "always()"
    assert "hindsight-bootstrap-apply-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in cleanup["run"]
    assert 'test -n "$RUNNER_TEMP"' in cleanup["run"]
    assert 'test "$RUNNER_TEMP" != "/"' in cleanup["run"]
    assert 'test "$RUNNER_TEMP" != "$GITHUB_WORKSPACE"' in cleanup["run"]
    assert 'ACTUAL_PLAN_DIR="${PLAN_DIR:-$EXPECTED_PLAN_DIR}"' in cleanup["run"]
    assert 'test "$ACTUAL_PLAN_DIR" = "$EXPECTED_PLAN_DIR"' in cleanup["run"]
    assert 'rm -rf -- "$EXPECTED_PLAN_DIR"' in cleanup["run"]
