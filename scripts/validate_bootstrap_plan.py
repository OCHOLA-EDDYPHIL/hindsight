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
TERRAFORM_VERSION = "1.13.5"
EXPECTED_REPOSITORY = "OCHOLA-EDDYPHIL/hindsight"
EXPECTED_WORKFLOW = ".github/workflows/plan-bootstrap.yml"
EXPECTED_AWS_ACCOUNT_ID = "762397612117"
ALLOWED_DATA_REMOVALS = frozenset({"data.aws_s3_bucket.state"})
ALLOWED_OUTPUT_REMOVALS = frozenset(
    {
        "learning_corpus_kms_key_alias",
        "learning_corpus_kms_key_arn",
    }
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


def _contains_true(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, dict):
        return any(_contains_true(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_true(child) for child in value)
    return False


def _has_sensitive_marker(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key == "sensitive"
                or key == "sensitive_values"
                or key.endswith("_sensitive")
            ) and _contains_true(child):
                return True
            if _has_sensitive_marker(child):
                return True
    elif isinstance(value, list):
        return any(_has_sensitive_marker(child) for child in value)
    return False


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

    cloudflare_managed = mode == "managed" and (
        resource_type.startswith("cloudflare_")
        or provider == "registry.terraform.io/cloudflare/cloudflare"
    )
    if cloudflare_managed and actions != ["no-op"]:
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
    if change.get("action_reason") is not None:
        summary["action_reason"] = change["action_reason"]
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


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate a full Terraform plan JSON document and summarize every action."""

    required = {
        "applyable",
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
    if _has_sensitive_marker(plan):
        raise ValueError("Terraform plan contains sensitive values and cannot be retained")

    changes = _resource_actions(plan["resource_changes"], kind="resource_changes")
    drift = _resource_actions(plan.get("resource_drift", []), kind="resource_drift")
    outputs = _output_actions(plan["output_changes"])
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
        "complete": True,
        "observed_allowed_removals": {
            "data_resources": observed_data_removals,
            "outputs": observed_output_removals,
        },
        "output_changes": outputs,
        "plan_format_version": format_version,
        "resource_changes": changes,
        "resource_drift": drift,
        "schema_version": SCHEMA_VERSION,
        "terraform_version": TERRAFORM_VERSION,
        "totals": {
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
