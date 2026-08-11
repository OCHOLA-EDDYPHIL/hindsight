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
            _resource("aws_iam_role.github_deploy", ["update"]),
            _resource(
                "cloudflare_dns_record.acm_validation",
                ["no-op"],
                resource_type="cloudflare_dns_record",
                provider="registry.terraform.io/cloudflare/cloudflare",
            ),
            _resource(
                "data.aws_s3_bucket.state",
                ["delete"],
                mode="data",
                resource_type="aws_s3_bucket",
            ),
        ],
        "resource_drift": [
            _resource("aws_iam_policy.github_deploy_observability", ["update"])
        ],
        "output_changes": {
            "github_deploy_role_arn": {
                "actions": ["no-op"],
                "before": "arn:old",
                "after": "arn:old",
                "before_sensitive": False,
                "after_sensitive": False,
            },
            "learning_corpus_kms_key_arn": {
                "actions": ["delete"],
                "before": "arn:retired",
                "after": None,
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


def _provenance(*, serial: int = 25):
    return {
        "lineage": "9f1383b4-4dae-30e4-bdba-649bc9346bc3",
        "serial": serial,
        "terraform_version": "1.15.8",
    }


def test_validator_records_every_resource_drift_and_output_action():
    validator = _validator()

    summary = validator.validate_plan(_plan(validator))

    assert [row["address"] for row in summary["resource_changes"]] == [
        "aws_iam_role.github_deploy",
        "cloudflare_dns_record.acm_validation",
        "data.aws_s3_bucket.state",
    ]
    assert [row["address"] for row in summary["resource_drift"]] == [
        "aws_iam_policy.github_deploy_observability"
    ]
    assert [row["name"] for row in summary["output_changes"]] == [
        "github_deploy_role_arn",
        "learning_corpus_kms_key_arn",
    ]
    assert summary["totals"] == {
        "checks": 2,
        "resource_changes": 3,
        "resource_drift": 1,
        "output_changes": 2,
    }
    assert summary["action_counts"] == {"delete": 2, "no-op": 2, "update": 2}
    assert summary["observed_allowed_removals"] == {
        "data_resources": ["data.aws_s3_bucket.state"],
        "outputs": ["learning_corpus_kms_key_arn"],
    }
    assert summary["null_sensitive_placeholders"] == []
    assert [row["to_display"] for row in summary["checks"]] == sorted(
        validator.REQUIRED_CHECKS
    )
    assert all(row["status"] == "pass" for row in summary["checks"])


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
    output["output_changes"]["github_deploy_role_arn"]["after_sensitive"] = True
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
def test_validator_accepts_only_explicitly_retired_outputs(output_name):
    validator = _validator()
    assert validator.ALLOWED_OUTPUT_REMOVALS == {
        "learning_corpus_kms_key_alias",
        "learning_corpus_kms_key_arn",
        "tenant_lifecycle_export_bucket_arn",
    }
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

    summary = validator.validate_plan(plan)

    assert summary["observed_allowed_removals"]["outputs"] == [output_name]


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
        "resource_changes": 3,
        "resource_drift": 1,
        "output_changes": 2,
    }

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


def test_bootstrap_plan_workflow_is_owner_gated_read_only_and_reviewable():
    workflow_path = ROOT / ".github/workflows/plan-bootstrap.yml"
    text = workflow_path.read_text()
    workflow = yaml.safe_load(text)
    jobs = workflow["jobs"]
    authorize = jobs["authorize"]
    plan = jobs["plan"]

    assert workflow["permissions"] == {}
    triggers = workflow.get("on", workflow.get(True))
    confirmation = triggers["workflow_dispatch"]["inputs"]["confirmation"]
    assert confirmation["required"] is True
    assert confirmation["type"] == "string"
    assert workflow["concurrency"] == {
        "group": "hindsight-bootstrap-plan-demo",
        "cancel-in-progress": False,
    }
    assert authorize["runs-on"] == "${{ vars.HINDSIGHT_RUNNER_LABEL }}"
    assert authorize["permissions"] == {}
    authorization = authorize["steps"][0]["run"]
    for required in (
        '"$EVENT_NAME" == "workflow_dispatch"',
        '"$REPOSITORY" == "OCHOLA-EDDYPHIL/hindsight"',
        '"$PRIVATE_REPOSITORY" == "true"',
        '"$REF_NAME" == "refs/heads/main"',
        '"$REF_PROTECTED" == "true"',
        '"$CONFIRMATION" == "plan-bootstrap-$EVENT_SHA"',
        '"$ACTOR" == "$REPOSITORY_OWNER"',
        '"$TRIGGERING_ACTOR" == "$REPOSITORY_OWNER"',
    ):
        assert required in authorization

    assert plan["runs-on"] == "${{ vars.HINDSIGHT_RUNNER_LABEL }}"
    assert plan["environment"] == "demo"
    assert plan["permissions"] == {"contents": "read", "id-token": "write"}
    assert plan["env"]["AWS_REGION"] == "us-east-1"
    assert plan["env"]["EXPECTED_AWS_ACCOUNT_ID"] == "762397612117"
    assert plan["env"]["CONFIGURED_AWS_ACCOUNT_ID"] == "${{ vars.AWS_ACCOUNT_ID }}"
    assert plan["env"]["TF_VAR_expected_aws_account_id"] == "762397612117"
    assert plan["env"]["TF_VAR_existing_github_oidc_provider_arn"] == (
        "arn:aws:iam::762397612117:oidc-provider/token.actions.githubusercontent.com"
    )
    assert plan["env"]["TF_STATE_KEY"] == "hindsight/bootstrap/terraform.tfstate"
    assert plan["env"]["PLAN_ROLE_ARN"] == "${{ vars.AWS_BOOTSTRAP_PLAN_ROLE_ARN }}"

    checkout = next(step for step in plan["steps"] if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"] == {
        "ref": "${{ needs.authorize.outputs.source_revision }}",
        "persist-credentials": False,
    }
    terraform = next(
        step for step in plan["steps"] if step.get("uses") == "hashicorp/setup-terraform@v3"
    )
    assert terraform["with"] == {
        "terraform_version": "1.13.5",
        "terraform_wrapper": False,
    }
    credentials = next(
        step
        for step in plan["steps"]
        if step.get("uses") == "aws-actions/configure-aws-credentials@v4"
    )
    assert credentials["with"]["role-to-assume"] == "${{ env.PLAN_ROLE_ARN }}"
    assert credentials["with"]["unset-current-credentials"] is True
    protected_configuration = next(
        step
        for step in plan["steps"]
        if step.get("name") == "Verify protected planning configuration"
    )["run"]
    assert 'test "$CONFIGURED_AWS_ACCOUNT_ID" = "$EXPECTED_AWS_ACCOUNT_ID"' in (
        protected_configuration
    )

    plan_step = next(
        step for step in plan["steps"] if step.get("name") == "Create full refreshed locked bootstrap plan"
    )
    assert plan_step["env"] == {
        "CLOUDFLARE_API_TOKEN": "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    }
    for flag in ("-input=false", "-lock=true", "-lock-timeout=5m", "-refresh=true"):
        assert flag in plan_step["run"]
    for forbidden in ("-target", "-destroy", "-refresh=false", " apply "):
        assert forbidden not in plan_step["run"]
    assert "terraform -chdir=infra/terraform/bootstrap apply" not in text
    assert "-target" not in text
    assert "-destroy" not in text
    assert "-refresh=false" not in text
    assert text.count("secrets.CLOUDFLARE_API_TOKEN") == 1
    assert "PLAN_DIR" not in plan["env"]
    assert "TF_DATA_DIR" not in plan["env"]
    assert "TF_PLUGIN_CACHE_DIR" not in plan["env"]
    assert all("runner.temp" not in str(value) for value in plan["env"].values())
    prepare = next(
        step for step in plan["steps"] if step.get("name") == "Prepare exact temporary workspace"
    )
    assert 'PLAN_DIR="$RUNNER_TEMP/hindsight-bootstrap-plan-' in prepare["run"]
    assert 'test "$TF_DATA_DIR" = "$PLAN_DIR/terraform-data"' in prepare["run"]
    assert 'mkdir -m 0700 -- "$PLAN_DIR/terraform-data"' in prepare["run"]
    assert 'printf \'PLAN_DIR=%s\\n\'' in prepare["run"]
    assert '>> "$GITHUB_ENV"' in prepare["run"]

    before = next(
        step for step in plan["steps"] if step.get("name") == "Record state provenance before planning"
    )
    after = next(
        step for step in plan["steps"] if step.get("name") == "Record state provenance after planning"
    )
    for step in (before, after):
        assert 'aws s3 cp "s3://$TF_STATE_BUCKET/$TF_STATE_KEY" -' in step["run"]
        assert "state-provenance" in step["run"]

    upload = next(
        step for step in plan["steps"] if step.get("uses") == "actions/upload-artifact@v4"
    )
    assert upload["with"]["retention-days"] == 1
    for artifact in (
        "bootstrap.tfplan",
        "bootstrap.tfplan.json",
        "bootstrap-plan-actions.json",
        "bootstrap-plan-manifest.json",
        "bootstrap.terraform.lock.hcl",
        "bootstrap-state-before.json",
        "bootstrap-state-after.json",
        "SHA256SUMS",
    ):
        assert artifact in upload["with"]["path"]
    cleanup = next(
        step for step in plan["steps"] if step.get("name") == "Remove exact temporary workspace"
    )
    assert cleanup["if"] == "always()"
    assert "hindsight-bootstrap-plan-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in cleanup["run"]
    assert 'test -n "$RUNNER_TEMP"' in cleanup["run"]
    assert 'test "$RUNNER_TEMP" != "/"' in cleanup["run"]
    assert 'test "$RUNNER_TEMP" != "$GITHUB_WORKSPACE"' in cleanup["run"]
    assert 'ACTUAL_PLAN_DIR="${PLAN_DIR:-$EXPECTED_PLAN_DIR}"' in cleanup["run"]
    assert 'test "$ACTUAL_PLAN_DIR" = "$EXPECTED_PLAN_DIR"' in cleanup["run"]
    assert 'rm -rf -- "$EXPECTED_PLAN_DIR"' in cleanup["run"]
    assert "sha256sum --check SHA256SUMS" in text
