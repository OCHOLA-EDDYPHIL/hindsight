"""Produce a fail-closed, reviewable manifest for a saved bootstrap plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "hindsight.bootstrap_plan.v1"
RECEIPT_SCHEMA_VERSION = "hindsight.bootstrap_apply_receipt.v1"
TERRAFORM_VERSION = "1.13.5"
EXPECTED_REPOSITORY = "OCHOLA-EDDYPHIL/hindsight"
EXPECTED_WORKFLOW = ".github/workflows/plan-bootstrap.yml"
EXPECTED_AWS_ACCOUNT_ID = "762397612117"
REQUIRED_CHECKS = frozenset(
    {
        "check.expected_aws_account",
        "check.state_bucket_scope",
    }
)
VALID_CHECK_KINDS = frozenset({"check", "output_value", "resource", "var"})
ALLOWED_DATA_REMOVALS = frozenset({"data.aws_s3_bucket.state"})
ALLOWED_OUTPUT_REMOVALS = frozenset(
    {
        "learning_corpus_kms_key_alias",
        "learning_corpus_kms_key_arn",
        "tenant_lifecycle_export_bucket_arn",
    }
)
AWS_PROVIDER = "registry.terraform.io/hashicorp/aws"
CLOUDFLARE_PROVIDER = "registry.terraform.io/cloudflare/cloudflare"
ALLOWED_CLOUDFLARE_REFRESH_DRIFT = {
    'cloudflare_dns_record.acm_validation["hindsight.strathmoreedu.qzz.io"]': {
        "index": "hindsight.strathmoreedu.qzz.io",
        "name": "acm_validation",
        "provider_name": CLOUDFLARE_PROVIDER,
        "type": "cloudflare_dns_record",
    }
}
GITHUB_DEPLOY_ROLE_REFRESH_DRIFT = {
    "address": "aws_iam_role.github_deploy",
    "mode": "managed",
    "name": "github_deploy",
    "provider_name": AWS_PROVIDER,
    "type": "aws_iam_role",
}
GITHUB_DEPLOY_ROLE_POLICY = {
    "address": "aws_iam_role_policy.github_deploy",
    "mode": "managed",
    "name": "github_deploy",
    "provider_name": AWS_PROVIDER,
    "type": "aws_iam_role_policy",
}
GITHUB_DEPLOY_INLINE_POLICY_NAME = "terraform-96877ae1e0309d9aea1db9eeb4"
GITHUB_DEPLOY_ROLE_IDENTITY = {
    "account_id": EXPECTED_AWS_ACCOUNT_ID,
    "name": "hindsight-github-deploy",
}
GITHUB_DEPLOY_ROLE_VALUE_IDENTITY = {
    "arn": (
        f"arn:aws:iam::{EXPECTED_AWS_ACCOUNT_ID}:role/hindsight-github-deploy"
    ),
    "id": "hindsight-github-deploy",
    "name": "hindsight-github-deploy",
}
GITHUB_DEPLOY_ROLE_VALUE_FIELDS = frozenset(
    {
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
)
GITHUB_DEPLOY_ROLE_SENSITIVE_VALUES = {
    "inline_policy": [{}],
    "managed_policy_arns": [False],
    "tags": {},
    "tags_all": {},
}
LIFECYCLE_EXPORT_BUCKET_ARNS = frozenset(
    {
        (
            "arn:aws:s3:::"
            f"hindsight-demo-lifecycle-exports-{EXPECTED_AWS_ACCOUNT_ID}"
        ),
        (
            "arn:aws:s3:::"
            f"hindsight-demo-lifecycle-exports-{EXPECTED_AWS_ACCOUNT_ID}/*"
        ),
    }
)
LIFECYCLE_RECOVERY_BUCKET_ARNS = frozenset(
    {
        f"arn:aws:s3:::hindsight-demo-recovery-{EXPECTED_AWS_ACCOUNT_ID}",
        f"arn:aws:s3:::hindsight-demo-recovery-{EXPECTED_AWS_ACCOUNT_ID}/*",
    }
)
LIFECYCLE_ARCHIVE_DENIED_ACTIONS = frozenset(
    {
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
)
GITHUB_DEPLOY_POLICY_STATEMENT_SIDS = frozenset(
    {
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
)
ALLOWED_NULL_SENSITIVE_RESOURCE_ATTRIBUTES = {
    "aws_acm_certificate.demo": {
        "attribute": "private_key",
        "index": None,
        "name": "demo",
        "provider_name": AWS_PROVIDER,
        "schema_version": 0,
        "type": "aws_acm_certificate",
    },
    "aws_s3_bucket_object_lock_configuration.learning_evidence[0]": {
        "attribute": "token",
        "index": 0,
        "name": "learning_evidence",
        "provider_name": AWS_PROVIDER,
        "schema_version": 0,
        "type": "aws_s3_bucket_object_lock_configuration",
    },
}
SENSITIVE_PLAN_ERROR = (
    "Terraform plan contains non-null, unknown, malformed, or unapproved "
    "sensitive values and cannot be retained"
)
VALID_ACTION_SEQUENCES = frozenset(
    {
        ("no-op",),
        ("create",),
        ("read",),
        ("update",),
        ("delete",),
        ("delete", "create"),
        ("create", "delete"),
        ("forget",),
    }
)
EXPECTED_ARTIFACT_NAMES = {
    "plan": "bootstrap.tfplan",
    "plan_json": "bootstrap.tfplan.json",
    "actions": "bootstrap-plan-actions.json",
    "manifest": "bootstrap-plan-manifest.json",
    "lock": "bootstrap.terraform.lock.hcl",
    "state_before": "bootstrap-state-before.json",
    "state_after": "bootstrap-state-after.json",
}
EXPECTED_RECEIPT_NAMES = {
    "manifest": EXPECTED_ARTIFACT_NAMES["manifest"],
    "actions": EXPECTED_ARTIFACT_NAMES["actions"],
    "state_before_apply": "bootstrap-state-before-apply.json",
    "state_after_apply": "bootstrap-state-after-apply.json",
    "state_after_postcheck": "bootstrap-state-after-postcheck.json",
    "receipt": "bootstrap-apply-receipt.json",
}
EXPECTED_PREAPPLY_NAMES = {
    "manifest": EXPECTED_ARTIFACT_NAMES["manifest"],
    "actions": EXPECTED_ARTIFACT_NAMES["actions"],
    "plan": EXPECTED_ARTIFACT_NAMES["plan"],
    "plan_json": EXPECTED_ARTIFACT_NAMES["plan_json"],
    "lock": EXPECTED_ARTIFACT_NAMES["lock"],
    "state_before_plan": EXPECTED_ARTIFACT_NAMES["state_before"],
    "state_after_plan": EXPECTED_ARTIFACT_NAMES["state_after"],
    "state_before_apply": EXPECTED_RECEIPT_NAMES["state_before_apply"],
}
MUTATING_ACTIONS = frozenset({"create", "update", "delete", "forget"})
ALLOWED_RESOURCE_MUTATIONS = frozenset(
    {
        ("aws_iam_policy.github_deploy_encryption", ("create",)),
        ("aws_iam_policy.github_deploy_observability", ("update",)),
        ("aws_iam_role.github_quarantine_redrive", ("create",)),
        ("aws_iam_role.github_worker_acceptance", ("create",)),
        ("aws_iam_role_policy.github_observability_evidence", ("update",)),
        ("aws_iam_role_policy.github_quarantine_redrive", ("create",)),
        ("aws_iam_role_policy.github_worker_acceptance", ("create",)),
        ("aws_iam_role_policy_attachment.github_deploy_encryption", ("create",)),
    }
)
ALLOWED_OUTPUT_MUTATIONS = frozenset(
    {
        ("github_quarantine_redrive_role_arn", ("create",)),
        ("github_worker_acceptance_role_arn", ("create",)),
    }
)


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return document


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_provenance(state: dict[str, Any]) -> dict[str, Any]:
    """Return only the non-sensitive identity fields needed to bind a state snapshot."""

    lineage = state.get("lineage")
    serial = state.get("serial")
    terraform_version = state.get("terraform_version")
    if not isinstance(lineage, str) or re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        lineage,
    ) is None:
        raise ValueError("Terraform state lineage must be a canonical UUID")
    if isinstance(serial, bool) or not isinstance(serial, int) or serial < 0:
        raise ValueError("Terraform state serial must be a non-negative integer")
    if not isinstance(terraform_version, str) or re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", terraform_version
    ) is None:
        raise ValueError("Terraform state must identify its Terraform version")
    return {
        "lineage": lineage,
        "serial": serial,
        "terraform_version": terraform_version,
    }


JsonPath = tuple[str | int, ...]


def _sensitive_marker_paths(
    value: Any,
    *,
    path: JsonPath = (),
    under_sensitive_marker: bool = False,
) -> set[JsonPath]:
    paths: set[JsonPath] = set()
    if under_sensitive_marker:
        if value is True:
            paths.add(path)
            return paths
        if value is False:
            return paths
        if not isinstance(value, (dict, list)):
            raise ValueError(SENSITIVE_PLAN_ERROR)
    if isinstance(value, dict):
        for key, child in value.items():
            child_under_sensitive_marker = under_sensitive_marker or (
                key == "sensitive"
                or key == "sensitive_values"
                or key.endswith("_sensitive")
            )
            paths.update(
                _sensitive_marker_paths(
                    child,
                    path=(*path, key),
                    under_sensitive_marker=child_under_sensitive_marker,
                )
            )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            paths.update(
                _sensitive_marker_paths(
                    child,
                    path=(*path, index),
                    under_sensitive_marker=under_sensitive_marker,
                )
            )
    return paths


def _validate_sensitive_resource_identity(
    entry: dict[str, Any],
    *,
    address: str,
    expected: dict[str, Any],
    value_entry: bool,
) -> None:
    if (
        entry.get("address") != address
        or entry.get("mode") != "managed"
        or entry.get("name") != expected["name"]
        or entry.get("provider_name") != expected["provider_name"]
        or entry.get("type") != expected["type"]
        or any(
            field in entry
            for field in ("deposed", "module_address", "previous_address")
        )
    ):
        raise ValueError(SENSITIVE_PLAN_ERROR)
    expected_index = expected["index"]
    if expected_index is None:
        if "index" in entry:
            raise ValueError(SENSITIVE_PLAN_ERROR)
    elif (
        isinstance(entry.get("index"), bool)
        or not isinstance(entry.get("index"), int)
        or entry["index"] != expected_index
    ):
        raise ValueError(SENSITIVE_PLAN_ERROR)
    if value_entry:
        if (
            isinstance(entry.get("schema_version"), bool)
            or not isinstance(entry.get("schema_version"), int)
            or entry["schema_version"] != expected["schema_version"]
        ):
            raise ValueError(SENSITIVE_PLAN_ERROR)
    elif "schema_version" in entry:
        raise ValueError(SENSITIVE_PLAN_ERROR)


def _value_resource_entries(
    values: Any,
    *,
    prefix: JsonPath,
) -> list[tuple[dict[str, Any], JsonPath]]:
    if not isinstance(values, dict) or not isinstance(values.get("root_module"), dict):
        return []

    entries: list[tuple[dict[str, Any], JsonPath]] = []

    def walk(module: dict[str, Any], module_path: JsonPath) -> None:
        resources = module.get("resources", [])
        if isinstance(resources, list):
            for index, resource in enumerate(resources):
                if isinstance(resource, dict):
                    entries.append((resource, (*module_path, "resources", index)))
        child_modules = module.get("child_modules", [])
        if isinstance(child_modules, list):
            for index, child in enumerate(child_modules):
                if isinstance(child, dict):
                    walk(child, (*module_path, "child_modules", index))

    walk(values["root_module"], (*prefix, "root_module"))
    return entries


def _single_sensitive_entry(
    entries: list[tuple[dict[str, Any], JsonPath]],
    *,
    address: str,
    parent_path: JsonPath,
) -> tuple[dict[str, Any], JsonPath]:
    matches = [(entry, path) for entry, path in entries if entry.get("address") == address]
    if len(matches) != 1 or matches[0][1][:-1] != parent_path:
        raise ValueError(SENSITIVE_PLAN_ERROR)
    return matches[0]


def _validate_null_value_marker(
    entry: dict[str, Any],
    *,
    entry_path: JsonPath,
    attribute: str,
) -> JsonPath:
    values = entry.get("values")
    masks = entry.get("sensitive_values")
    if (
        not isinstance(values, dict)
        or attribute not in values
        or values[attribute] is not None
        or not isinstance(masks, dict)
        or masks.get(attribute) is not True
    ):
        raise ValueError(SENSITIVE_PLAN_ERROR)
    return (*entry_path, "sensitive_values", attribute)


def _validate_null_change_markers(
    entry: dict[str, Any],
    *,
    entry_path: JsonPath,
    attribute: str,
) -> set[JsonPath]:
    change = entry.get("change")
    if not isinstance(change, dict):
        raise ValueError(SENSITIVE_PLAN_ERROR)
    paths: set[JsonPath] = set()
    for marker_name, value_name in (
        ("before_sensitive", "before"),
        ("after_sensitive", "after"),
    ):
        values = change.get(value_name)
        masks = change.get(marker_name)
        if (
            not isinstance(values, dict)
            or attribute not in values
            or values[attribute] is not None
            or not isinstance(masks, dict)
            or masks.get(attribute) is not True
        ):
            raise ValueError(SENSITIVE_PLAN_ERROR)
        paths.add((*entry_path, "change", marker_name, attribute))

    after_unknown = change.get("after_unknown")
    if not isinstance(after_unknown, dict) or (
        attribute in after_unknown and after_unknown[attribute] is not False
    ):
        raise ValueError(SENSITIVE_PLAN_ERROR)
    return paths


def _validate_sensitive_markers(plan: dict[str, Any]) -> list[dict[str, str]]:
    observed_paths = _sensitive_marker_paths(plan)
    planned_entries = _value_resource_entries(
        plan.get("planned_values"), prefix=("planned_values",)
    )
    prior_state = plan.get("prior_state")
    prior_entries = _value_resource_entries(
        prior_state.get("values") if isinstance(prior_state, dict) else None,
        prefix=("prior_state", "values"),
    )
    resource_changes = plan.get("resource_changes")
    change_entries = (
        [
            (entry, ("resource_changes", index))
            for index, entry in enumerate(resource_changes)
            if isinstance(entry, dict)
        ]
        if isinstance(resource_changes, list)
        else []
    )
    resource_drift = plan.get("resource_drift", [])
    drift_entries = (
        [
            (entry, ("resource_drift", index))
            for index, entry in enumerate(resource_drift)
            if isinstance(entry, dict)
        ]
        if isinstance(resource_drift, list)
        else []
    )
    drift_addresses = (
        {entry.get("address") for entry, _ in drift_entries}
    )

    all_entries = [
        *planned_entries,
        *prior_entries,
        *change_entries,
        *drift_entries,
    ]
    sensitive_resource_types = {
        expected["type"]
        for expected in ALLOWED_NULL_SENSITIVE_RESOURCE_ATTRIBUTES.values()
    }
    for entry, _ in all_entries:
        if (
            entry.get("type") in sensitive_resource_types
            and entry.get("address")
            not in ALLOWED_NULL_SENSITIVE_RESOURCE_ATTRIBUTES
        ):
            raise ValueError(SENSITIVE_PLAN_ERROR)

    allowed_paths: set[JsonPath] = set()
    identities: list[dict[str, str]] = []
    for address, expected in ALLOWED_NULL_SENSITIVE_RESOURCE_ATTRIBUTES.items():
        address_occurs = any(
            entry.get("address") == address
            for entry, _ in [*planned_entries, *prior_entries, *change_entries]
        ) or address in drift_addresses
        if not address_occurs:
            continue
        if address in drift_addresses:
            raise ValueError(SENSITIVE_PLAN_ERROR)

        planned, planned_path = _single_sensitive_entry(
            planned_entries,
            address=address,
            parent_path=("planned_values", "root_module", "resources"),
        )
        prior, prior_path = _single_sensitive_entry(
            prior_entries,
            address=address,
            parent_path=("prior_state", "values", "root_module", "resources"),
        )
        change, change_path = _single_sensitive_entry(
            change_entries,
            address=address,
            parent_path=("resource_changes",),
        )
        _validate_sensitive_resource_identity(
            planned,
            address=address,
            expected=expected,
            value_entry=True,
        )
        _validate_sensitive_resource_identity(
            prior,
            address=address,
            expected=expected,
            value_entry=True,
        )
        _validate_sensitive_resource_identity(
            change,
            address=address,
            expected=expected,
            value_entry=False,
        )

        attribute = expected["attribute"]
        allowed_paths.add(
            _validate_null_value_marker(
                planned,
                entry_path=planned_path,
                attribute=attribute,
            )
        )
        allowed_paths.add(
            _validate_null_value_marker(
                prior,
                entry_path=prior_path,
                attribute=attribute,
            )
        )
        allowed_paths.update(
            _validate_null_change_markers(
                change,
                entry_path=change_path,
                attribute=attribute,
            )
        )
        identities.append({"address": address, "attribute": attribute})

    if observed_paths != allowed_paths:
        raise ValueError(SENSITIVE_PLAN_ERROR)
    return identities


def _actions(change: Any, *, subject: str) -> list[str]:
    if not isinstance(change, dict):
        raise ValueError(f"{subject} is missing its change object")
    actions = change.get("actions")
    if (
        not isinstance(actions, list)
        or not actions
        or any(not isinstance(action, str) for action in actions)
        or tuple(actions) not in VALID_ACTION_SEQUENCES
    ):
        raise ValueError(f"{subject} has an invalid Terraform action list")
    return actions


def _resource_summary(entry: Any, *, kind: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError(f"{kind} entries must be JSON objects")
    address = entry.get("address")
    mode = entry.get("mode")
    resource_type = entry.get("type")
    provider = entry.get("provider_name")
    name = entry.get("name")
    if not all(isinstance(value, str) and value for value in (address, resource_type, name)):
        raise ValueError(f"{kind} entry has an incomplete resource identity")
    if mode not in {"managed", "data"}:
        raise ValueError(f"{kind} entry {address} has unsupported mode {mode!r}")
    if not isinstance(provider, str) or not provider:
        raise ValueError(f"{kind} entry {address} is missing its provider identity")

    change = entry.get("change")
    actions = _actions(change, subject=f"{kind} entry {address}")
    assert isinstance(change, dict)
    has_delete = "delete" in actions or "forget" in actions
    replacement = "create" in actions and has_delete

    if kind == "resource_drift" and has_delete:
        raise ValueError(f"resource drift deletes are forbidden: {address}")
    if replacement:
        raise ValueError(f"resource replacements are forbidden: {address}")
    if mode == "managed" and has_delete:
        raise ValueError(f"managed resource removals are forbidden: {address}")
    if mode == "data" and has_delete and not (
        address in ALLOWED_DATA_REMOVALS and actions == ["delete"] and kind == "resource_changes"
    ):
        raise ValueError(f"unexpected data resource removal: {address}")

    cloudflare_resource = _cloudflare_resource(entry)
    if kind == "resource_changes" and cloudflare_resource:
        managed_change = mode == "managed" and (
            actions != ["no-op"]
            or not _resource_entry_is_refresh_only(entry, change)
        )
        invalid_data_action = mode == "data" and actions not in (
            ["no-op"],
            ["read"],
        )
        if managed_change or invalid_data_action:
            raise ValueError(f"Cloudflare managed changes are forbidden: {address}")

    summary: dict[str, Any] = {
        "actions": actions,
        "address": address,
        "mode": mode,
        "name": name,
        "provider_name": provider,
        "type": resource_type,
    }
    for key in ("module_address", "previous_address", "deposed", "index"):
        if key in entry:
            summary[key] = entry[key]
    if entry.get("action_reason") is not None:
        summary["action_reason"] = entry["action_reason"]
    if change.get("replace_paths"):
        summary["replace_paths"] = change["replace_paths"]
    if change.get("importing") is not None:
        summary["importing"] = True
    return summary


def _resource_actions(entries: Any, *, kind: str) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise ValueError(f"Terraform plan {kind} must be a complete list")
    summaries = [_resource_summary(entry, kind=kind) for entry in entries]
    identities = [
        (summary["address"], json.dumps(summary.get("deposed"), sort_keys=True))
        for summary in summaries
    ]
    if len(set(identities)) != len(identities):
        raise ValueError(f"Terraform plan {kind} contains duplicate resource identities")
    return sorted(
        summaries,
        key=lambda row: (row["address"], str(row.get("deposed", ""))),
    )


def _cloudflare_resource(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    address = entry.get("address")
    return (
        (
            isinstance(address, str)
            and (
                address in ALLOWED_CLOUDFLARE_REFRESH_DRIFT
                or re.search(r"(?:^|\.)cloudflare_[^.]+\.", address) is not None
            )
        )
        or str(entry.get("type", "")).startswith("cloudflare_")
        or entry.get("provider_name") == CLOUDFLARE_PROVIDER
    )


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
    except (TypeError, ValueError):
        raise ValueError(
            "Cloudflare refresh drift contains invalid JSON values"
        ) from None


def _unknown_values_clear(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, dict):
        return all(_unknown_values_clear(child) for child in value.values())
    if isinstance(value, list):
        return all(_unknown_values_clear(child) for child in value)
    return False


def _change_is_refresh_only(change: dict[str, Any]) -> bool:
    return (
        change.get("action_reason") is None
        and change.get("importing") is None
        and change.get("replace_paths") in (None, [])
        and _unknown_values_clear(change.get("after_unknown", {}))
    )


def _resource_entry_is_refresh_only(
    entry: dict[str, Any], change: dict[str, Any]
) -> bool:
    return entry.get("action_reason") is None and _change_is_refresh_only(change)


def _validate_cloudflare_refresh_drift(
    resource_changes: list[Any],
    resource_drift: list[Any],
) -> list[str]:
    observed: list[str] = []
    for drift_entry in resource_drift:
        if not _cloudflare_resource(drift_entry):
            continue
        address = drift_entry.get("address")
        expected = ALLOWED_CLOUDFLARE_REFRESH_DRIFT.get(address)
        if expected is None:
            raise ValueError(f"unapproved Cloudflare refresh drift: {address}")
        matching_changes = [
            entry
            for entry in resource_changes
            if isinstance(entry, dict) and entry.get("address") == address
        ]
        if len(matching_changes) != 1:
            raise ValueError(
                f"Cloudflare refresh drift is not uniquely reconciled: {address}"
            )
        change_entry = matching_changes[0]
        for entry in (drift_entry, change_entry):
            if (
                entry.get("mode") != "managed"
                or entry.get("name") != expected["name"]
                or entry.get("provider_name") != expected["provider_name"]
                or entry.get("type") != expected["type"]
                or entry.get("index") != expected["index"]
                or any(
                    field in entry
                    for field in (
                        "deposed",
                        "module_address",
                        "previous_address",
                        "schema_version",
                    )
                )
            ):
                raise ValueError(
                    f"Cloudflare refresh drift identity is invalid: {address}"
                )

        drift_change = drift_entry.get("change")
        desired_change = change_entry.get("change")
        if not isinstance(drift_change, dict) or not isinstance(desired_change, dict):
            raise ValueError(f"Cloudflare refresh drift is incomplete: {address}")
        if not _resource_entry_is_refresh_only(
            drift_entry, drift_change
        ) or not _resource_entry_is_refresh_only(change_entry, desired_change):
            raise ValueError(
                f"Cloudflare refresh drift contains additional behavior: {address}"
            )
        drift_before = drift_change.get("before")
        refreshed = drift_change.get("after")
        desired_before = desired_change.get("before")
        desired_after = desired_change.get("after")
        if (
            _actions(drift_change, subject=f"Cloudflare refresh drift {address}")
            != ["update"]
            or _actions(
                desired_change, subject=f"Cloudflare desired state {address}"
            )
            != ["no-op"]
            or not all(
                isinstance(value, dict)
                for value in (
                    drift_before,
                    refreshed,
                    desired_before,
                    desired_after,
                )
            )
        ):
            raise ValueError(f"Cloudflare refresh drift is not a no-op: {address}")
        canonical_values = tuple(
            _canonical_json(value)
            for value in (
                drift_before,
                refreshed,
                desired_before,
                desired_after,
            )
        )
        if (
            canonical_values[0] == canonical_values[1]
            or canonical_values[1] != canonical_values[2]
            or canonical_values[2] != canonical_values[3]
        ):
            raise ValueError(f"Cloudflare refresh drift is not a no-op: {address}")
        observed.append(address)
    return sorted(observed)


def _canonical_refresh_json(value: Any) -> str:
    try:
        return json.dumps(
            value, allow_nan=False, separators=(",", ":"), sort_keys=True
        )
    except (TypeError, ValueError):
        raise ValueError(
            "GitHub deploy role refresh drift contains invalid JSON values"
        ) from None


def _load_inline_policy_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        raise ValueError("GitHub deploy role refresh drift policy is malformed")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, child in pairs:
            if key in document:
                raise ValueError
            document[key] = child
        return document

    def reject_non_json_constant(_: str) -> None:
        raise ValueError

    try:
        document = json.loads(
            value,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_json_constant,
        )
    except (TypeError, ValueError):
        raise ValueError(
            "GitHub deploy role refresh drift policy is malformed"
        ) from None
    if (
        not isinstance(document, dict)
        or set(document) != {"Statement", "Version"}
        or document.get("Version") != "2012-10-17"
        or not isinstance(document.get("Statement"), list)
        or len(document["Statement"]) != 14
        or not all(isinstance(statement, dict) for statement in document["Statement"])
    ):
        raise ValueError("GitHub deploy role refresh drift policy is malformed")
    statement_sids = [statement.get("Sid") for statement in document["Statement"]]
    if (
        not all(isinstance(sid, str) and sid for sid in statement_sids)
        or len(statement_sids) != len(set(statement_sids))
        or frozenset(statement_sids) != GITHUB_DEPLOY_POLICY_STATEMENT_SIDS
    ):
        raise ValueError(
            "GitHub deploy role refresh drift statement identities are invalid"
        )
    return document


def _inline_policy_snapshot(
    role_values: dict[str, Any],
) -> tuple[str, dict[str, Any], int]:
    inline_policies = role_values.get("inline_policy")
    if (
        not isinstance(inline_policies, list)
        or len(inline_policies) != 1
        or not isinstance(inline_policies[0], dict)
        or set(inline_policies[0]) != {"name", "policy"}
    ):
        raise ValueError(
            "GitHub deploy role refresh drift must contain one exact inline policy"
        )
    policy_name = inline_policies[0].get("name")
    if policy_name != GITHUB_DEPLOY_INLINE_POLICY_NAME:
        raise ValueError(
            "GitHub deploy role refresh drift inline policy identity is invalid"
        )
    policy = _load_inline_policy_document(inline_policies[0].get("policy"))
    target_indexes = [
        index
        for index, statement in enumerate(policy["Statement"])
        if statement.get("Sid") == "LifecycleArchiveMutationDenied"
    ]
    if len(target_indexes) != 1:
        raise ValueError(
            "GitHub deploy role refresh drift target statement is not unique"
        )
    return policy_name, policy, target_indexes[0]


def _validate_lifecycle_statement(
    statement: dict[str, Any], *, expected_resources: frozenset[str]
) -> None:
    actions = statement.get("Action")
    resources = statement.get("Resource")
    if (
        set(statement) != {"Action", "Effect", "Resource", "Sid"}
        or statement.get("Sid") != "LifecycleArchiveMutationDenied"
        or statement.get("Effect") != "Deny"
        or not isinstance(actions, list)
        or not all(isinstance(action, str) and action for action in actions)
        or len(actions) != len(set(actions))
        or frozenset(actions) != LIFECYCLE_ARCHIVE_DENIED_ACTIONS
        or not isinstance(resources, list)
        or not all(isinstance(resource, str) for resource in resources)
        or len(resources) != len(set(resources))
        or frozenset(resources) != expected_resources
    ):
        raise ValueError(
            "GitHub deploy role refresh drift target statement is invalid"
        )


def _policy_without_transition_resources(
    policy: dict[str, Any], target_index: int
) -> dict[str, Any]:
    statements = list(policy["Statement"])
    target = dict(statements[target_index])
    target["Resource"] = "<lifecycle-archive-transition>"
    statements[target_index] = target
    return {"Statement": statements, "Version": policy["Version"]}


def _validate_inline_policy_transition(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    before_name, before_policy, before_target = _inline_policy_snapshot(before)
    after_name, after_policy, after_target = _inline_policy_snapshot(after)
    if before_name != after_name or before_target != after_target:
        raise ValueError(
            "GitHub deploy role refresh drift inline policy identity changed"
        )
    _validate_lifecycle_statement(
        before_policy["Statement"][before_target],
        expected_resources=LIFECYCLE_EXPORT_BUCKET_ARNS,
    )
    _validate_lifecycle_statement(
        after_policy["Statement"][after_target],
        expected_resources=(
            LIFECYCLE_EXPORT_BUCKET_ARNS | LIFECYCLE_RECOVERY_BUCKET_ARNS
        ),
    )
    if _canonical_refresh_json(
        _policy_without_transition_resources(before_policy, before_target)
    ) != _canonical_refresh_json(
        _policy_without_transition_resources(after_policy, after_target)
    ):
        raise ValueError(
            "GitHub deploy role refresh drift changed other policy content"
        )
    return after_name, after_policy


def _validate_role_values_identity(values: dict[str, Any]) -> None:
    if set(values) != GITHUB_DEPLOY_ROLE_VALUE_FIELDS or any(
        values.get(field) != expected
        for field, expected in GITHUB_DEPLOY_ROLE_VALUE_IDENTITY.items()
    ):
        raise ValueError("GitHub deploy role refresh drift value identity is invalid")


def _validate_role_refresh_entry(
    entry: Any,
    *,
    actions: list[str],
    desired_change: bool,
) -> dict[str, Any]:
    if not isinstance(entry, dict) or set(entry) != {
        "address",
        "change",
        "mode",
        "name",
        "provider_name",
        "type",
    }:
        raise ValueError("GitHub deploy role refresh drift identity is invalid")
    if any(
        entry.get(field) != expected
        for field, expected in GITHUB_DEPLOY_ROLE_REFRESH_DRIFT.items()
    ):
        raise ValueError("GitHub deploy role refresh drift identity is invalid")

    change = entry.get("change")
    expected_change_fields = {
        "actions",
        "after",
        "after_sensitive",
        "after_unknown",
        "before",
        "before_sensitive",
    }
    if desired_change:
        expected_change_fields.update({"after_identity", "before_identity"})
    if not isinstance(change, dict) or set(change) != expected_change_fields:
        raise ValueError(
            "GitHub deploy role refresh drift contains unexpected metadata"
        )
    if _actions(change, subject="GitHub deploy role refresh drift") != actions:
        raise ValueError("GitHub deploy role refresh drift actions are invalid")
    if change.get("after_unknown") != {}:
        raise ValueError(
            "GitHub deploy role refresh drift contains unknown values"
        )
    if desired_change and (
        change.get("before_identity") != GITHUB_DEPLOY_ROLE_IDENTITY
        or change.get("after_identity") != GITHUB_DEPLOY_ROLE_IDENTITY
    ):
        raise ValueError(
            "GitHub deploy role refresh drift change identity is invalid"
        )
    before = change.get("before")
    after = change.get("after")
    before_sensitive = change.get("before_sensitive")
    after_sensitive = change.get("after_sensitive")
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or not isinstance(before_sensitive, dict)
        or not isinstance(after_sensitive, dict)
        or before_sensitive != GITHUB_DEPLOY_ROLE_SENSITIVE_VALUES
        or after_sensitive != GITHUB_DEPLOY_ROLE_SENSITIVE_VALUES
    ):
        raise ValueError("GitHub deploy role refresh drift snapshot is malformed")
    _validate_role_values_identity(before)
    _validate_role_values_identity(after)
    return change


def _validate_github_deploy_role_policy_change(
    resource_changes: list[Any],
    resource_drift: list[Any],
    *,
    policy_name: str,
    refreshed_policy: dict[str, Any],
) -> None:
    address = GITHUB_DEPLOY_ROLE_POLICY["address"]
    if any(
        isinstance(entry, dict) and entry.get("address") == address
        for entry in resource_drift
    ):
        raise ValueError("GitHub deploy inline policy drift is forbidden")
    matching_changes = [
        entry
        for entry in resource_changes
        if isinstance(entry, dict) and entry.get("address") == address
    ]
    if len(matching_changes) != 1:
        raise ValueError(
            "GitHub deploy role refresh drift is not bound to one inline policy"
        )
    entry = matching_changes[0]
    if set(entry) != {
        "address",
        "change",
        "mode",
        "name",
        "provider_name",
        "type",
    } or any(
        entry.get(field) != expected
        for field, expected in GITHUB_DEPLOY_ROLE_POLICY.items()
    ):
        raise ValueError("GitHub deploy inline policy identity is invalid")

    change = entry.get("change")
    if not isinstance(change, dict) or set(change) != {
        "actions",
        "after",
        "after_identity",
        "after_sensitive",
        "after_unknown",
        "before",
        "before_identity",
        "before_sensitive",
    }:
        raise ValueError("GitHub deploy inline policy contains unexpected metadata")
    if (
        _actions(change, subject="GitHub deploy inline policy") != ["no-op"]
        or change.get("after_unknown") != {}
        or change.get("before_sensitive") != {}
        or change.get("after_sensitive") != {}
    ):
        raise ValueError("GitHub deploy inline policy no-op is invalid")
    expected_identity = {
        "account_id": EXPECTED_AWS_ACCOUNT_ID,
        "name": policy_name,
        "role": GITHUB_DEPLOY_ROLE_VALUE_IDENTITY["name"],
    }
    if (
        change.get("before_identity") != expected_identity
        or change.get("after_identity") != expected_identity
    ):
        raise ValueError("GitHub deploy inline policy change identity is invalid")

    before = change.get("before")
    after = change.get("after")
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or set(before) != {"id", "name", "name_prefix", "policy", "role"}
        or _canonical_refresh_json(before) != _canonical_refresh_json(after)
        or before.get("id")
        != f"{GITHUB_DEPLOY_ROLE_VALUE_IDENTITY['name']}:{policy_name}"
        or before.get("name") != policy_name
        or before.get("name_prefix") != "terraform-"
        or before.get("role") != GITHUB_DEPLOY_ROLE_VALUE_IDENTITY["name"]
    ):
        raise ValueError("GitHub deploy inline policy no-op is invalid")
    policy = _load_inline_policy_document(before.get("policy"))
    if _canonical_refresh_json(policy) != _canonical_refresh_json(refreshed_policy):
        raise ValueError(
            "GitHub deploy role refresh drift does not match its inline policy"
        )


def _validate_github_deploy_role_refresh_drift(
    resource_changes: list[Any],
    resource_drift: list[Any],
) -> list[str]:
    address = GITHUB_DEPLOY_ROLE_REFRESH_DRIFT["address"]
    drift_entries = [
        entry
        for entry in resource_drift
        if isinstance(entry, dict) and entry.get("address") == address
    ]
    if not drift_entries:
        return []
    if len(drift_entries) != 1:
        raise ValueError(
            "GitHub deploy role refresh drift is not uniquely represented"
        )
    matching_changes = [
        entry
        for entry in resource_changes
        if isinstance(entry, dict) and entry.get("address") == address
    ]
    if len(matching_changes) != 1:
        raise ValueError(
            "GitHub deploy role refresh drift is not uniquely reconciled"
        )

    drift_change = _validate_role_refresh_entry(
        drift_entries[0], actions=["update"], desired_change=False
    )
    desired_change = _validate_role_refresh_entry(
        matching_changes[0], actions=["no-op"], desired_change=True
    )
    drift_before = drift_change["before"]
    refreshed = drift_change["after"]
    desired_before = desired_change["before"]
    desired_after = desired_change["after"]
    if set(drift_before) != set(refreshed) or _canonical_refresh_json(
        {key: value for key, value in drift_before.items() if key != "inline_policy"}
    ) != _canonical_refresh_json(
        {key: value for key, value in refreshed.items() if key != "inline_policy"}
    ):
        raise ValueError("GitHub deploy role refresh drift changed other role values")
    policy_name, refreshed_policy = _validate_inline_policy_transition(
        drift_before, refreshed
    )
    if (
        _canonical_refresh_json(refreshed)
        != _canonical_refresh_json(desired_before)
        or _canonical_refresh_json(desired_before)
        != _canonical_refresh_json(desired_after)
        or drift_change["after_sensitive"]
        != desired_change["before_sensitive"]
    ):
        raise ValueError(
            "GitHub deploy role refresh drift is not a matching desired no-op"
        )
    _validate_github_deploy_role_policy_change(
        resource_changes,
        resource_drift,
        policy_name=policy_name,
        refreshed_policy=refreshed_policy,
    )
    return [address]


def _output_actions(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, dict):
        raise ValueError("Terraform plan output_changes must be a complete object")
    summaries: list[dict[str, Any]] = []
    for name, change in entries.items():
        if not isinstance(name, str) or not name:
            raise ValueError("Terraform plan contains an invalid output name")
        actions = _actions(change, subject=f"output {name}")
        has_delete = "delete" in actions or "forget" in actions
        if "create" in actions and has_delete:
            raise ValueError(f"output replacement is forbidden: {name}")
        if has_delete and not (name in ALLOWED_OUTPUT_REMOVALS and actions == ["delete"]):
            raise ValueError(f"unexpected output removal: {name}")
        summaries.append({"actions": actions, "name": name})
    return sorted(summaries, key=lambda row: row["name"])


def _action_counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(action for entry in entries for action in entry["actions"])
    return {action: counts[action] for action in sorted(counts)}


def _validate_desired_state_mutations(
    resource_changes: list[dict[str, Any]],
    output_changes: list[dict[str, Any]],
) -> None:
    for entry in resource_changes:
        actions = tuple(entry["actions"])
        if not MUTATING_ACTIONS.isdisjoint(actions) and (
            entry["address"], actions
        ) not in ALLOWED_RESOURCE_MUTATIONS:
            raise ValueError(
                "unapproved desired-state resource mutation: "
                f"{entry['address']} ({', '.join(actions)})"
            )

    for entry in output_changes:
        actions = tuple(entry["actions"])
        if not MUTATING_ACTIONS.isdisjoint(actions) and (
            entry["name"], actions
        ) not in ALLOWED_OUTPUT_MUTATIONS:
            raise ValueError(
                "unapproved desired-state output mutation: "
                f"{entry['name']} ({', '.join(actions)})"
            )


def _check_identity(address: Any, *, subject: str, require_kind: bool) -> dict[str, str]:
    if not isinstance(address, dict):
        raise ValueError(f"{subject} is missing its address object")
    to_display = address.get("to_display")
    if not isinstance(to_display, str) or not to_display or len(to_display) > 512:
        raise ValueError(f"{subject} has an invalid display address")
    identity = {"to_display": to_display}
    if require_kind:
        kind = address.get("kind")
        name = address.get("name")
        if kind not in VALID_CHECK_KINDS:
            raise ValueError(f"{subject} has an unsupported check kind")
        if not isinstance(name, str) or not name or len(name) > 256:
            raise ValueError(f"{subject} has an invalid check name")
        identity.update({"kind": kind, "name": name})
        if kind == "resource":
            mode = address.get("mode")
            resource_type = address.get("type")
            if mode not in {"data", "managed"}:
                raise ValueError(f"{subject} has an unsupported resource mode")
            if (
                not isinstance(resource_type, str)
                or not resource_type
                or len(resource_type) > 256
            ):
                raise ValueError(f"{subject} has an invalid resource type")
            expected_suffix = f"{resource_type}.{name}"
            identity.update({"mode": mode, "type": resource_type})
        else:
            prefix = {
                "check": "check",
                "output_value": "output",
                "var": "var",
            }[kind]
            expected_suffix = f"{prefix}.{name}"
        if to_display != expected_suffix and not to_display.endswith(
            f".{expected_suffix}"
        ):
            raise ValueError(f"{subject} address is inconsistent")
    return identity


def _check_summaries(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list) or not entries:
        raise ValueError("Terraform plan checks must be a complete non-empty list")
    summaries: list[dict[str, Any]] = []
    aggregate_addresses: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Terraform plan check entries must be JSON objects")
        identity = _check_identity(
            entry.get("address"), subject="Terraform plan check", require_kind=True
        )
        display = identity["to_display"]
        if display in aggregate_addresses:
            raise ValueError(f"Terraform plan contains duplicate check identity: {display}")
        aggregate_addresses.add(display)
        if entry.get("status") != "pass":
            raise ValueError(f"Terraform plan check did not pass: {display}")
        if entry.get("problems") not in (None, []):
            raise ValueError(f"Terraform plan check contains problems: {display}")
        instances = entry.get("instances", [])
        if not isinstance(instances, list) or (
            not instances and identity["kind"] != "resource"
        ):
            raise ValueError(f"Terraform plan check instances are incomplete: {display}")
        instance_summaries: list[dict[str, str]] = []
        instance_addresses: set[str] = set()
        for instance in instances:
            if not isinstance(instance, dict):
                raise ValueError(f"Terraform plan check instance is malformed: {display}")
            instance_identity = _check_identity(
                instance.get("address"),
                subject=f"Terraform plan check instance {display}",
                require_kind=False,
            )
            instance_display = instance_identity["to_display"]
            if instance_display != display and not instance_display.startswith(
                f"{display}["
            ):
                raise ValueError(
                    f"Terraform plan check instance address is inconsistent: {instance_display}"
                )
            if instance_display in instance_addresses:
                raise ValueError(
                    f"Terraform plan contains duplicate check instance: {instance_display}"
                )
            instance_addresses.add(instance_display)
            if instance.get("status") != "pass":
                raise ValueError(
                    f"Terraform plan check instance did not pass: {instance_display}"
                )
            if instance.get("problems") not in (None, []):
                raise ValueError(
                    f"Terraform plan check instance contains problems: {instance_display}"
                )
            instance_summaries.append(
                {"status": "pass", "to_display": instance_display}
            )
        summaries.append(
            {
                **identity,
                "instances": sorted(
                    instance_summaries, key=lambda row: row["to_display"]
                ),
                "status": "pass",
            }
        )
    observed_required = {
        summary["to_display"]
        for summary in summaries
        if summary["to_display"] in REQUIRED_CHECKS
        and summary["kind"] == "check"
        and summary["name"] == summary["to_display"].removeprefix("check.")
    }
    missing = sorted(REQUIRED_CHECKS - observed_required)
    if missing:
        raise ValueError(
            "Terraform plan is missing required checks: " + ", ".join(missing)
        )
    return sorted(summaries, key=lambda row: row["to_display"])


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate a full Terraform plan JSON document and summarize every action."""

    required = {
        "applyable",
        "checks",
        "complete",
        "configuration",
        "errored",
        "format_version",
        "output_changes",
        "planned_values",
        "resource_changes",
        "terraform_version",
    }
    missing = sorted(required - plan.keys())
    if missing:
        raise ValueError(f"Terraform plan JSON is incomplete: missing {', '.join(missing)}")
    if plan.get("complete") is not True or plan.get("errored") is not False:
        raise ValueError("Terraform plan must be complete and error-free")
    if not isinstance(plan.get("applyable"), bool):
        raise ValueError("Terraform plan applyable status must be explicit")
    if not isinstance(plan.get("configuration"), dict) or not isinstance(
        plan.get("planned_values"), dict
    ):
        raise ValueError("Terraform plan configuration and planned values must be complete")
    if plan.get("terraform_version") != TERRAFORM_VERSION:
        raise ValueError(f"Terraform plan must be produced by Terraform {TERRAFORM_VERSION}")
    format_version = plan.get("format_version")
    if not isinstance(format_version, str) or re.fullmatch(r"1\.[0-9]+", format_version) is None:
        raise ValueError("Terraform plan JSON format is unsupported")
    null_sensitive_placeholders = _validate_sensitive_markers(plan)

    checks = _check_summaries(plan["checks"])
    changes = _resource_actions(plan["resource_changes"], kind="resource_changes")
    drift = _resource_actions(plan.get("resource_drift", []), kind="resource_drift")
    reconciled_refresh_drift = sorted(
        [
            *_validate_cloudflare_refresh_drift(
                plan["resource_changes"], plan.get("resource_drift", [])
            ),
            *_validate_github_deploy_role_refresh_drift(
                plan["resource_changes"], plan.get("resource_drift", [])
            ),
        ]
    )
    outputs = _output_actions(plan["output_changes"])
    unapproved_drift = sorted(
        entry["address"]
        for entry in drift
        if entry["address"] not in reconciled_refresh_drift
    )
    if unapproved_drift:
        raise ValueError(
            "unapproved resource drift is forbidden: "
            + ", ".join(unapproved_drift)
        )
    _validate_desired_state_mutations(changes, outputs)
    all_entries = [*changes, *drift, *outputs]
    observed_data_removals = sorted(
        entry["address"]
        for entry in changes
        if entry["mode"] == "data" and entry["actions"] == ["delete"]
    )
    observed_output_removals = sorted(
        entry["name"] for entry in outputs if entry["actions"] == ["delete"]
    )
    return {
        "action_counts": _action_counts(all_entries),
        "applyable": plan["applyable"],
        "checks": checks,
        "complete": True,
        "observed_allowed_removals": {
            "data_resources": observed_data_removals,
            "outputs": observed_output_removals,
        },
        "null_sensitive_placeholders": null_sensitive_placeholders,
        "reconciled_refresh_drift": reconciled_refresh_drift,
        "output_changes": outputs,
        "plan_format_version": format_version,
        "resource_changes": changes,
        "resource_drift": drift,
        "schema_version": SCHEMA_VERSION,
        "terraform_version": TERRAFORM_VERSION,
        "totals": {
            "checks": len(checks),
            "output_changes": len(outputs),
            "resource_changes": len(changes),
            "resource_drift": len(drift),
        },
    }


def _load_provenance(path: Path) -> dict[str, Any]:
    provenance = _load_json(path)
    if set(provenance) != {"lineage", "serial", "terraform_version"}:
        raise ValueError(f"{path.name} contains fields beyond state provenance")
    return state_provenance(provenance)


def _require_exact_fields(
    document: dict[str, Any], expected: set[str], *, subject: str
) -> None:
    if set(document) != expected:
        raise ValueError(f"{subject} has an unexpected structure")


def _validate_digest_map(digests: Any, *, expected_names: set[str]) -> dict[str, str]:
    if not isinstance(digests, dict) or set(digests) != expected_names:
        raise ValueError("bootstrap plan manifest artifact set is incomplete")
    for name, digest in digests.items():
        if not isinstance(name, str) or not isinstance(digest, str) or re.fullmatch(
            r"[0-9a-f]{64}", digest
        ) is None:
            raise ValueError("bootstrap plan manifest contains an invalid artifact digest")
    return digests


def _validated_receipt_actions(
    actions: dict[str, Any], manifest_plan: Any
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    expected_fields = {
        "action_counts",
        "applyable",
        "checks",
        "complete",
        "observed_allowed_removals",
        "null_sensitive_placeholders",
        "reconciled_refresh_drift",
        "output_changes",
        "plan_format_version",
        "resource_changes",
        "resource_drift",
        "schema_version",
        "terraform_version",
        "totals",
    }
    _require_exact_fields(actions, expected_fields, subject="bootstrap plan actions")
    if (
        actions.get("schema_version") != SCHEMA_VERSION
        or actions.get("terraform_version") != TERRAFORM_VERSION
        or actions.get("complete") is not True
        or not isinstance(actions.get("applyable"), bool)
    ):
        raise ValueError("bootstrap plan actions metadata is invalid")

    collections: dict[str, list[dict[str, Any]]] = {}
    for name in ("resource_changes", "resource_drift", "output_changes"):
        entries = actions.get(name)
        if not isinstance(entries, list) or not all(
            isinstance(entry, dict) for entry in entries
        ):
            raise ValueError(f"bootstrap plan actions {name} is incomplete")
        for entry in entries:
            sequence = entry.get("actions")
            if (
                not isinstance(sequence, list)
                or not all(isinstance(action, str) for action in sequence)
                or tuple(sequence) not in VALID_ACTION_SEQUENCES
            ):
                raise ValueError(f"bootstrap plan actions {name} contains invalid actions")
        collections[name] = entries

    observed_counts = _action_counts(
        [
            *collections["resource_changes"],
            *collections["resource_drift"],
            *collections["output_changes"],
        ]
    )
    if actions.get("action_counts") != observed_counts:
        raise ValueError("bootstrap plan action counts are inconsistent")

    totals = actions.get("totals")
    expected_totals = {
        "checks": len(actions["checks"]) if isinstance(actions.get("checks"), list) else -1,
        "output_changes": len(collections["output_changes"]),
        "resource_changes": len(collections["resource_changes"]),
        "resource_drift": len(collections["resource_drift"]),
    }
    if totals != expected_totals:
        raise ValueError("bootstrap plan action totals are inconsistent")

    expected_manifest_plan = {
        "action_counts": observed_counts,
        "applyable": actions["applyable"],
        "complete": True,
        "format_version": actions["plan_format_version"],
        "null_sensitive_placeholders": actions["null_sensitive_placeholders"],
        "reconciled_refresh_drift": actions["reconciled_refresh_drift"],
        "terraform_version": TERRAFORM_VERSION,
        "totals": expected_totals,
    }
    if manifest_plan != expected_manifest_plan:
        raise ValueError("bootstrap plan manifest does not match its actions")

    mutation_counter = Counter(
        action
        for entry in (
            *collections["resource_changes"],
            *collections["output_changes"],
        )
        for action in entry["actions"]
        if action in MUTATING_ACTIONS
    )
    mutation_counts = {
        action: mutation_counter[action] for action in sorted(mutation_counter)
    }
    if actions["applyable"] is not bool(mutation_counts):
        raise ValueError(
            "bootstrap plan applyable status is inconsistent with desired-state mutations"
        )
    return observed_counts, mutation_counts, expected_totals


def _detailed_exit_code(value: str, *, subject: str) -> int:
    if value not in {"0", "2"}:
        raise ValueError(f"{subject} must be an exact Terraform detailed exit code")
    return int(value)


def validate_preapply(
    *,
    manifest_file: Path,
    actions_file: Path,
    plan_file: Path,
    plan_json_file: Path,
    lock_file: Path,
    state_before_plan_file: Path,
    state_after_plan_file: Path,
    state_before_apply_file: Path,
    plan_exit_code: str,
) -> None:
    """Revalidate the exact saved plan and state immediately before apply."""

    paths = {
        "manifest": manifest_file,
        "actions": actions_file,
        "plan": plan_file,
        "plan_json": plan_json_file,
        "lock": lock_file,
        "state_before_plan": state_before_plan_file,
        "state_after_plan": state_after_plan_file,
        "state_before_apply": state_before_apply_file,
    }
    for kind, path in paths.items():
        if path.name != EXPECTED_PREAPPLY_NAMES[kind]:
            raise ValueError(f"unexpected {kind} artifact name: {path.name}")
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"{path.name} is missing or empty")

    manifest = _load_json(manifest_file)
    _require_exact_fields(
        manifest,
        {
            "artifacts",
            "aws",
            "environment",
            "plan",
            "repository",
            "run_attempt",
            "run_id",
            "schema_version",
            "source_revision",
            "state_provenance",
            "workflow_ref",
        },
        subject="bootstrap plan manifest",
    )
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("bootstrap plan manifest schema is invalid")

    artifact_paths = (
        plan_file,
        plan_json_file,
        actions_file,
        lock_file,
        state_before_plan_file,
        state_after_plan_file,
    )
    expected_artifact_names = set(EXPECTED_ARTIFACT_NAMES.values()) - {
        EXPECTED_ARTIFACT_NAMES["manifest"]
    }
    manifest_artifacts = _validate_digest_map(
        manifest.get("artifacts"), expected_names=expected_artifact_names
    )
    current_artifacts = {path.name: _sha256(path) for path in artifact_paths}
    if manifest_artifacts != current_artifacts:
        raise ValueError("saved bootstrap plan artifacts no longer match their manifest")

    actions = _load_json(actions_file)
    _, mutation_counts, _ = _validated_receipt_actions(
        actions, manifest.get("plan")
    )
    plan_code = _detailed_exit_code(plan_exit_code, subject="initial plan exit code")
    expected_plan_code = 2 if mutation_counts else 0
    if plan_code != expected_plan_code:
        raise ValueError(
            "initial plan exit code is inconsistent with applyable desired-state mutations"
        )

    manifest_state = manifest.get("state_provenance")
    if not isinstance(manifest_state, dict) or set(manifest_state) != {
        "before",
        "after",
    }:
        raise ValueError("bootstrap plan manifest state provenance is incomplete")
    if not all(
        isinstance(manifest_state[name], dict)
        and set(manifest_state[name]) == {"lineage", "serial", "terraform_version"}
        for name in ("before", "after")
    ):
        raise ValueError("bootstrap plan manifest state provenance is incomplete")
    plan_before = state_provenance(manifest_state["before"])
    plan_after = state_provenance(manifest_state["after"])
    if plan_before != plan_after:
        raise ValueError("bootstrap plan manifest state provenance changed during planning")
    if (
        _load_provenance(state_before_plan_file) != plan_before
        or _load_provenance(state_after_plan_file) != plan_after
    ):
        raise ValueError("bootstrap plan provenance files do not match their manifest")
    if _load_provenance(state_before_apply_file) != plan_after:
        raise ValueError("state before apply does not match the saved plan provenance")


def build_receipt(
    *,
    manifest_file: Path,
    actions_file: Path,
    state_before_apply_file: Path,
    state_after_apply_file: Path,
    state_after_postcheck_file: Path,
    receipt_file: Path,
    source_revision: str,
    repository: str,
    workflow_ref: str,
    aws_account_id: str,
    environment: str,
    plan_role_arn: str,
    apply_role_arn: str,
    run_id: str,
    run_attempt: str,
    plan_exit_code: str,
    postcheck_exit_code: str,
) -> dict[str, Any]:
    """Bind a plan and state transitions into a provenance-only apply receipt."""

    paths = {
        "manifest": manifest_file,
        "actions": actions_file,
        "state_before_apply": state_before_apply_file,
        "state_after_apply": state_after_apply_file,
        "state_after_postcheck": state_after_postcheck_file,
        "receipt": receipt_file,
    }
    for kind, path in paths.items():
        if path.name != EXPECTED_RECEIPT_NAMES[kind]:
            raise ValueError(f"unexpected {kind} artifact name: {path.name}")
    for kind in (
        "manifest",
        "actions",
        "state_before_apply",
        "state_after_apply",
        "state_after_postcheck",
    ):
        path = paths[kind]
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"{path.name} is missing or empty")

    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    if repository != EXPECTED_REPOSITORY:
        raise ValueError("bootstrap apply repository identity is not authorized")
    expected_workflow_ref = f"{EXPECTED_REPOSITORY}/{EXPECTED_WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_workflow_ref:
        raise ValueError("bootstrap apply workflow identity is not authorized")
    if aws_account_id != EXPECTED_AWS_ACCOUNT_ID:
        raise ValueError("bootstrap apply AWS account identity is not authorized")
    if environment != "demo":
        raise ValueError("bootstrap apply must use the demo environment")
    expected_plan_role = (
        f"arn:aws:iam::{aws_account_id}:role/hindsight-github-bootstrap-plan"
    )
    expected_apply_role = (
        f"arn:aws:iam::{aws_account_id}:role/hindsight-github-bootstrap-apply"
    )
    if plan_role_arn != expected_plan_role:
        raise ValueError("bootstrap plan role identity is not authorized")
    if apply_role_arn != expected_apply_role:
        raise ValueError("bootstrap apply role identity is not authorized")
    if re.fullmatch(r"[1-9][0-9]*", run_id) is None or re.fullmatch(
        r"[1-9][0-9]*", run_attempt
    ) is None:
        raise ValueError("workflow run identity must use positive decimal values")

    plan_code = _detailed_exit_code(plan_exit_code, subject="initial plan exit code")
    if postcheck_exit_code != "0":
        raise ValueError("post-apply check must report detailed exit code 0")

    manifest = _load_json(manifest_file)
    _require_exact_fields(
        manifest,
        {
            "artifacts",
            "aws",
            "environment",
            "plan",
            "repository",
            "run_attempt",
            "run_id",
            "schema_version",
            "source_revision",
            "state_provenance",
            "workflow_ref",
        },
        subject="bootstrap plan manifest",
    )
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("source_revision") != source_revision
        or manifest.get("repository") != repository
        or manifest.get("workflow_ref") != workflow_ref
        or isinstance(manifest.get("run_id"), bool)
        or not isinstance(manifest.get("run_id"), int)
        or manifest.get("run_id") != int(run_id)
        or isinstance(manifest.get("run_attempt"), bool)
        or not isinstance(manifest.get("run_attempt"), int)
        or manifest.get("run_attempt") != int(run_attempt)
        or manifest.get("environment") != environment
    ):
        raise ValueError("bootstrap plan manifest execution identity does not match")
    if manifest.get("aws") != {
        "account_id": aws_account_id,
        "region": "us-east-1",
        "role_arn": plan_role_arn,
    }:
        raise ValueError("bootstrap plan manifest AWS identity does not match")

    expected_manifest_artifacts = set(EXPECTED_ARTIFACT_NAMES.values()) - {
        EXPECTED_ARTIFACT_NAMES["manifest"]
    }
    manifest_artifacts = _validate_digest_map(
        manifest.get("artifacts"), expected_names=expected_manifest_artifacts
    )
    actions_digest = _sha256(actions_file)
    if manifest_artifacts[actions_file.name] != actions_digest:
        raise ValueError("bootstrap plan actions digest does not match its manifest")

    actions = _load_json(actions_file)
    action_counts, mutation_counts, totals = _validated_receipt_actions(
        actions, manifest.get("plan")
    )
    expected_plan_code = 2 if mutation_counts else 0
    if plan_code != expected_plan_code:
        raise ValueError(
            "initial plan exit code is inconsistent with applyable desired-state mutations"
        )

    manifest_state = manifest.get("state_provenance")
    if not isinstance(manifest_state, dict) or set(manifest_state) != {
        "before",
        "after",
    }:
        raise ValueError("bootstrap plan manifest state provenance is incomplete")
    if not all(
        isinstance(manifest_state[name], dict)
        and set(manifest_state[name]) == {"lineage", "serial", "terraform_version"}
        for name in ("before", "after")
    ):
        raise ValueError("bootstrap plan manifest state provenance is incomplete")
    plan_before = state_provenance(manifest_state["before"])
    plan_after = state_provenance(manifest_state["after"])
    if plan_before != plan_after:
        raise ValueError("bootstrap plan manifest state provenance changed during planning")

    state_before_apply = _load_provenance(state_before_apply_file)
    state_after_apply = _load_provenance(state_after_apply_file)
    state_after_postcheck = _load_provenance(state_after_postcheck_file)
    if state_before_apply != plan_after:
        raise ValueError("state before apply does not match the saved plan provenance")
    if (
        state_after_apply["lineage"] != state_before_apply["lineage"]
        or state_after_apply["terraform_version"]
        != state_before_apply["terraform_version"]
    ):
        raise ValueError("state lineage or Terraform version changed during apply")
    if plan_code == 2:
        if state_after_apply["serial"] <= state_before_apply["serial"]:
            raise ValueError("applied state serial did not increase")
    elif state_after_apply["serial"] != state_before_apply["serial"]:
        raise ValueError("no-op plan changed the state serial")
    if state_after_postcheck != state_after_apply:
        raise ValueError("state changed during the post-apply check")

    receipt = {
        "artifacts": {
            actions_file.name: actions_digest,
            manifest_file.name: _sha256(manifest_file),
        },
        "aws": {
            "account_id": aws_account_id,
            "apply_role_arn": apply_role_arn,
            "plan_role_arn": plan_role_arn,
            "region": "us-east-1",
        },
        "environment": environment,
        "plan": {
            "action_counts": action_counts,
            "applied": plan_code == 2,
            "applyable": actions["applyable"],
            "detailed_exit_code": plan_code,
            "mutation_action_counts": mutation_counts,
            "totals": totals,
        },
        "postcheck": {"detailed_exit_code": 0, "succeeded": True},
        "repository": repository,
        "run_attempt": int(run_attempt),
        "run_id": int(run_id),
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "source_revision": source_revision,
        "state_provenance": {
            "after_apply": state_after_apply,
            "after_postcheck": state_after_postcheck,
            "before_apply": state_before_apply,
            "plan": plan_after,
        },
        "workflow_ref": workflow_ref,
    }
    _write_json(receipt_file, receipt)
    return receipt


def build_manifest(
    *,
    plan_json: Path,
    plan_file: Path,
    lock_file: Path,
    state_before_file: Path,
    state_after_file: Path,
    actions_file: Path,
    manifest_file: Path,
    source_revision: str,
    repository: str,
    workflow_ref: str,
    aws_account_id: str,
    environment: str,
    role_arn: str,
    run_id: str,
    run_attempt: str,
) -> dict[str, Any]:
    paths = {
        "plan": plan_file,
        "plan_json": plan_json,
        "actions": actions_file,
        "manifest": manifest_file,
        "lock": lock_file,
        "state_before": state_before_file,
        "state_after": state_after_file,
    }
    for kind, path in paths.items():
        if path.name != EXPECTED_ARTIFACT_NAMES[kind]:
            raise ValueError(f"unexpected {kind} artifact name: {path.name}")
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    if repository != EXPECTED_REPOSITORY:
        raise ValueError("bootstrap plan repository identity is not authorized")
    expected_workflow_ref = f"{EXPECTED_REPOSITORY}/{EXPECTED_WORKFLOW}@refs/heads/main"
    if workflow_ref != expected_workflow_ref:
        raise ValueError("bootstrap plan workflow identity is not authorized")
    if aws_account_id != EXPECTED_AWS_ACCOUNT_ID:
        raise ValueError("bootstrap plan AWS account identity is not authorized")
    if environment != "demo":
        raise ValueError("bootstrap planning must use the demo environment")
    if role_arn != f"arn:aws:iam::{aws_account_id}:role/hindsight-github-bootstrap-plan":
        raise ValueError("bootstrap plan role identity is not authorized")
    if re.fullmatch(r"[1-9][0-9]*", run_id) is None or re.fullmatch(
        r"[1-9][0-9]*", run_attempt
    ) is None:
        raise ValueError("workflow run identity must use positive decimal values")
    for kind in ("plan", "plan_json", "lock", "state_before", "state_after"):
        path = paths[kind]
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"{path.name} is missing or empty")

    before = _load_provenance(state_before_file)
    after = _load_provenance(state_after_file)
    if before != after:
        raise ValueError("bootstrap state changed while the saved plan was produced")
    actions = validate_plan(_load_json(plan_json))
    _write_json(actions_file, actions)

    artifact_digests = {
        path.name: _sha256(path)
        for path in (
            plan_file,
            plan_json,
            actions_file,
            lock_file,
            state_before_file,
            state_after_file,
        )
    }
    manifest = {
        "artifacts": dict(sorted(artifact_digests.items())),
        "aws": {
            "account_id": aws_account_id,
            "region": "us-east-1",
            "role_arn": role_arn,
        },
        "environment": environment,
        "plan": {
            "action_counts": actions["action_counts"],
            "applyable": actions["applyable"],
            "complete": actions["complete"],
            "format_version": actions["plan_format_version"],
            "null_sensitive_placeholders": actions["null_sensitive_placeholders"],
            "reconciled_refresh_drift": actions["reconciled_refresh_drift"],
            "terraform_version": actions["terraform_version"],
            "totals": actions["totals"],
        },
        "repository": repository,
        "run_attempt": int(run_attempt),
        "run_id": int(run_id),
        "schema_version": SCHEMA_VERSION,
        "source_revision": source_revision,
        "state_provenance": {"after": after, "before": before},
        "workflow_ref": workflow_ref,
    }
    _write_json(manifest_file, manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    provenance = commands.add_parser("state-provenance")
    provenance.add_argument("--output", required=True, type=Path)

    plan = commands.add_parser("plan")
    plan.add_argument("--input", required=True, type=Path)
    plan.add_argument("--plan-file", required=True, type=Path)
    plan.add_argument("--lock-file", required=True, type=Path)
    plan.add_argument("--state-before", required=True, type=Path)
    plan.add_argument("--state-after", required=True, type=Path)
    plan.add_argument("--actions-output", required=True, type=Path)
    plan.add_argument("--manifest-output", required=True, type=Path)
    plan.add_argument("--source-revision", required=True)
    plan.add_argument("--repository", required=True)
    plan.add_argument("--workflow-ref", required=True)
    plan.add_argument("--aws-account-id", required=True)
    plan.add_argument("--environment", required=True)
    plan.add_argument("--role-arn", required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--run-attempt", required=True)

    preapply = commands.add_parser("preapply")
    preapply.add_argument("--manifest", required=True, type=Path)
    preapply.add_argument("--actions", required=True, type=Path)
    preapply.add_argument("--plan-file", required=True, type=Path)
    preapply.add_argument("--plan-json", required=True, type=Path)
    preapply.add_argument("--lock-file", required=True, type=Path)
    preapply.add_argument("--state-before-plan", required=True, type=Path)
    preapply.add_argument("--state-after-plan", required=True, type=Path)
    preapply.add_argument("--state-before-apply", required=True, type=Path)
    preapply.add_argument("--plan-exit-code", required=True)

    receipt = commands.add_parser("receipt")
    receipt.add_argument("--manifest", required=True, type=Path)
    receipt.add_argument("--actions", required=True, type=Path)
    receipt.add_argument("--state-before-apply", required=True, type=Path)
    receipt.add_argument("--state-after-apply", required=True, type=Path)
    receipt.add_argument("--state-after-postcheck", required=True, type=Path)
    receipt.add_argument("--output", required=True, type=Path)
    receipt.add_argument("--source-revision", required=True)
    receipt.add_argument("--repository", required=True)
    receipt.add_argument("--workflow-ref", required=True)
    receipt.add_argument("--aws-account-id", required=True)
    receipt.add_argument("--environment", required=True)
    receipt.add_argument("--plan-role-arn", required=True)
    receipt.add_argument("--apply-role-arn", required=True)
    receipt.add_argument("--run-id", required=True)
    receipt.add_argument("--run-attempt", required=True)
    receipt.add_argument("--plan-exit-code", required=True)
    receipt.add_argument("--postcheck-exit-code", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "state-provenance":
            state = json.load(sys.stdin)
            if not isinstance(state, dict):
                raise ValueError("Terraform state must contain one JSON object")
            _write_json(args.output, state_provenance(state))
            return 0
        if args.command == "preapply":
            validate_preapply(
                manifest_file=args.manifest,
                actions_file=args.actions,
                plan_file=args.plan_file,
                plan_json_file=args.plan_json,
                lock_file=args.lock_file,
                state_before_plan_file=args.state_before_plan,
                state_after_plan_file=args.state_after_plan,
                state_before_apply_file=args.state_before_apply,
                plan_exit_code=args.plan_exit_code,
            )
            return 0
        if args.command == "receipt":
            build_receipt(
                manifest_file=args.manifest,
                actions_file=args.actions,
                state_before_apply_file=args.state_before_apply,
                state_after_apply_file=args.state_after_apply,
                state_after_postcheck_file=args.state_after_postcheck,
                receipt_file=args.output,
                source_revision=args.source_revision,
                repository=args.repository,
                workflow_ref=args.workflow_ref,
                aws_account_id=args.aws_account_id,
                environment=args.environment,
                plan_role_arn=args.plan_role_arn,
                apply_role_arn=args.apply_role_arn,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                plan_exit_code=args.plan_exit_code,
                postcheck_exit_code=args.postcheck_exit_code,
            )
            return 0
        build_manifest(
            plan_json=args.input,
            plan_file=args.plan_file,
            lock_file=args.lock_file,
            state_before_file=args.state_before,
            state_after_file=args.state_after,
            actions_file=args.actions_output,
            manifest_file=args.manifest_output,
            source_revision=args.source_revision,
            repository=args.repository,
            workflow_ref=args.workflow_ref,
            aws_account_id=args.aws_account_id,
            environment=args.environment,
            role_arn=args.role_arn,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"bootstrap plan validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
