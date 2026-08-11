"""Create and verify exact-main migration and recovery evidence bundles."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any, Mapping
from uuid import UUID
import zipfile


AUTHORIZED_DOCUMENT_PATHS = (
    "README.md",
    "docs/development.md",
    "docs/api-security.md",
    "docs/architecture.md",
    "docs/operations.md",
)
GIT_INPUT_SCHEMA = "hindsight.git_input_manifest.v1"
MIGRATION_HISTORY_SCHEMA = "hindsight.migration_compatibility_history.v1"
MIGRATION_WORKLOAD_SCHEMA = "hindsight.migration_compatibility.v1"
DOMAIN_RECEIPT_SCHEMA = "hindsight.exact_main_evidence_receipt.v1"
ARTIFACT_MANIFEST_SCHEMA = "hindsight.exact_main_artifact_manifest.v1"
REUSE_RECEIPT_SCHEMA = "hindsight.evidence_reuse.v1"
RECOVERY_WORKLOAD_SCHEMA = "hindsight.recovery_drill.v1"
RECOVERY_LIMITATIONS = [
    "The workflow uses one local CockroachDB node and does not exercise node or region loss.",
    "The backup uses userfile storage on the same ephemeral cluster, so it does not prove "
    "off-cluster media durability.",
    "The drill simulates logical source-database loss; it does not simulate disk, network, "
    "credential, encryption-key, or control-plane failure.",
    "The measured intervals are client-observed on a small migrated fixture and are not "
    "production RPO, RTO, capacity, or SLO claims.",
]
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
MIGRATION_CASES = (
    "agent_runtime_roles",
    "dispatch_upgrade",
    "populated_roles",
    "prompt_safety_upgrade",
    "qualification_authority",
    "tenant_vector_index",
)
DOMAIN_SPECS = {
    "migration": {
        "workflow_path": ".github/workflows/migration-compatibility.yml",
        "artifact_prefix": "migration-compatibility",
        "workload_filename": "migration-compatibility.json",
        "receipt_filename": "migration-receipt.json",
    },
    "recovery": {
        "workflow_path": ".github/workflows/recovery-drill.yml",
        "artifact_prefix": "recovery-drill",
        "workload_filename": "recovery-drill.json",
        "receipt_filename": "recovery-receipt.json",
    },
}
INPUT_MANIFEST_FILENAME = "git-input-manifest.json"
INFRASTRUCTURE_CLEANUP_FILENAME = "infrastructure-cleanup.json"
ARTIFACT_MANIFEST_FILENAME = "artifact-manifest.json"
TARGET_MANIFEST_FILENAME = "target-git-input-manifest.json"
REUSE_RECEIPT_FILENAME = "evidence-reuse-receipt.json"
REUSE_CHECKSUM_FILENAME = "evidence-reuse-receipt.sha256"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _document_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _documents_equal(left: Any, right: Any) -> bool:
    try:
        return _canonical_bytes(left) == _canonical_bytes(right)
    except (TypeError, ValueError):
        return False


def _bytes_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _file_record(value: bytes) -> dict[str, Any]:
    return {"sha256": _bytes_sha256(value), "size_bytes": len(value)}


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_json_bytes(value: bytes, *, label: str) -> dict[str, Any]:
    def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        document: dict[str, Any] = {}
        for key, item in pairs:
            if key in document:
                raise ValueError("duplicate JSON object key")
            document[key] = item
        return document

    def reject_nonfinite(number: str) -> None:
        raise ValueError(f"nonfinite JSON number: {number}")

    try:
        document = json.loads(
            value,
            object_pairs_hook=object_without_duplicates,
            parse_constant=reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return document


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    return _load_json_bytes(path.read_bytes(), label=label)


def _require_exact_keys(value: Any, expected: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} does not have the exact required fields")
    return value


def _require_sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a full lowercase Git SHA")
    return value


def _require_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a full lowercase SHA-256 digest")
    return value


def _require_positive_int(value: Any, *, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _repository_owner(repository: str) -> str:
    parts = repository.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repository must be an owner/name pair")
    return parts[0]


def _git(
    repository_root: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["git", "-C", str(repository_root), *arguments],
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        suffix = f": {detail[-1][:300]}" if detail else ""
        raise RuntimeError(f"git {' '.join(arguments[:2])} failed{suffix}")
    return completed


def _resolve_commit(repository_root: Path, revision: str) -> str:
    _require_sha(revision, label="revision")
    resolved = _git(repository_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    value = resolved.stdout.decode("ascii").strip()
    if value != revision:
        raise ValueError("revision does not resolve to the exact requested commit")
    return value


def _tree_rows(repository_root: Path, revision: str) -> list[dict[str, str]]:
    output = _git(repository_root, "ls-tree", "-rz", "--full-tree", revision).stdout
    rows: list[dict[str, str]] = []
    for raw_row in output.split(b"\0"):
        if not raw_row:
            continue
        try:
            metadata, raw_path = raw_row.split(b"\t", 1)
            mode, object_type, object_id = metadata.decode("ascii").split(" ")
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Git tree contains a noncanonical entry") from exc
        if (
            not path
            or path.startswith("/")
            or "\0" in path
            or PurePosixPath(path).is_absolute()
            or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
        ):
            raise ValueError("Git tree contains a noncanonical path")
        valid_blob = object_type == "blob" and mode in {"100644", "100755", "120000"}
        valid_gitlink = object_type == "commit" and mode == "160000"
        if not (valid_blob or valid_gitlink) or SHA_PATTERN.fullmatch(object_id) is None:
            raise ValueError(f"Git tree entry has unsupported mode or type: {path}")
        rows.append(
            {
                "path": path,
                "mode": mode,
                "object_type": object_type,
                "object_id": object_id,
            }
        )
    if len({row["path"] for row in rows}) != len(rows):
        raise ValueError("Git tree contains duplicate paths")
    return rows


def _entry_identity(repository_root: Path, row: Mapping[str, str]) -> tuple[int, str]:
    if row["object_type"] == "blob":
        value = _git(repository_root, "cat-file", "blob", row["object_id"]).stdout
    else:
        value = bytes.fromhex(row["object_id"])
    return len(value), _bytes_sha256(value)


def build_git_manifest(repository_root: Path, revision: str) -> dict[str, Any]:
    """Return a canonical manifest for all non-authorized-document Git entries."""

    revision = _resolve_commit(repository_root, revision)
    rows = _tree_rows(repository_root, revision)
    documents = {row["path"]: row for row in rows if row["path"] in AUTHORIZED_DOCUMENT_PATHS}
    if set(documents) != set(AUTHORIZED_DOCUMENT_PATHS) or any(
        row["mode"] != "100644" or row["object_type"] != "blob"
        for row in documents.values()
    ):
        raise ValueError("authorized documentation paths must be tracked regular files")

    entries: list[dict[str, Any]] = []
    for row in rows:
        if row["path"] in AUTHORIZED_DOCUMENT_PATHS:
            continue
        content_size, identity_sha256 = _entry_identity(repository_root, row)
        entries.append(
            {
                **row,
                "content_size_bytes": content_size,
                "content_identity_sha256": identity_sha256,
            }
        )
    entries.sort(key=lambda item: item["path"].encode("utf-8"))
    identity = {
        "excluded_paths": list(AUTHORIZED_DOCUMENT_PATHS),
        "entries": entries,
    }
    manifest: dict[str, Any] = {
        "schema_version": GIT_INPUT_SCHEMA,
        "source_revision": revision,
        **identity,
        "entry_count": len(entries),
        "inputs_sha256": _document_sha256(identity),
    }
    manifest["manifest_sha256"] = _document_sha256(manifest)
    return manifest


def validate_git_manifest(
    document: dict[str, Any],
    *,
    repository_root: Path,
    revision: str,
) -> dict[str, Any]:
    expected = build_git_manifest(repository_root, revision)
    if not _documents_equal(document, expected):
        raise ValueError("Git input manifest does not match the exact repository tree")
    return expected


def write_git_manifest(repository_root: Path, revision: str, output: Path) -> dict[str, Any]:
    manifest = build_git_manifest(repository_root, revision)
    _write_json(output, manifest)
    validate_git_manifest(
        _load_json(output, label="Git input manifest"),
        repository_root=repository_root,
        revision=revision,
    )
    return manifest


def verify_documentation_delta(
    repository_root: Path,
    *,
    source_revision: str,
    target_revision: str,
) -> list[dict[str, str]]:
    """Verify a nonempty, regular-file-only delta within the exact authorized path set."""

    source_revision = _resolve_commit(repository_root, source_revision)
    target_revision = _resolve_commit(repository_root, target_revision)
    if source_revision == target_revision:
        raise ValueError("evidence reuse requires a nonempty documentation delta")
    ancestry = _git(
        repository_root,
        "merge-base",
        "--is-ancestor",
        source_revision,
        target_revision,
        check=False,
    )
    if ancestry.returncode != 0:
        raise ValueError("source revision is not an ancestor of the exact target revision")

    raw = _git(
        repository_root,
        "diff",
        "--raw",
        "--abbrev=40",
        "-z",
        "--no-renames",
        source_revision,
        target_revision,
        "--",
    ).stdout
    fields = raw.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if not fields or len(fields) % 2:
        raise ValueError("documentation delta is empty or malformed")

    changes: list[dict[str, str]] = []
    for index in range(0, len(fields), 2):
        try:
            header = fields[index].decode("ascii")
            path = fields[index + 1].decode("utf-8")
            old_mode, new_mode, old_id, new_id, status = header.removeprefix(":").split(" ")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("documentation delta is malformed") from exc
        if (
            not header.startswith(":")
            or status != "M"
            or old_mode != "100644"
            or new_mode != "100644"
            or path not in AUTHORIZED_DOCUMENT_PATHS
            or SHA_PATTERN.fullmatch(old_id) is None
            or SHA_PATTERN.fullmatch(new_id) is None
        ):
            raise ValueError("delta contains a disallowed path, mode, type, or status")
        old_content = _git(repository_root, "cat-file", "blob", old_id).stdout
        new_content = _git(repository_root, "cat-file", "blob", new_id).stdout
        changes.append(
            {
                "path": path,
                "old_mode": old_mode,
                "new_mode": new_mode,
                "object_type": "blob",
                "old_object_id": old_id,
                "new_object_id": new_id,
                "old_content_sha256": _bytes_sha256(old_content),
                "new_content_sha256": _bytes_sha256(new_content),
            }
        )
    changes.sort(key=lambda item: item["path"].encode("utf-8"))
    return changes


def _validate_historical_migration(
    document: dict[str, Any],
    *,
    source_revision: str,
    run_id: int,
    run_attempt: int,
) -> list[dict[str, Any]]:
    _require_exact_keys(
        document,
        {"schema_version", "status", "source_revision", "workflow_run", "cases"},
        label="historical migration evidence",
    )
    workflow_run = _require_exact_keys(
        document["workflow_run"], {"id", "attempt"}, label="migration workflow run"
    )
    cases = document["cases"]
    if (
        document["schema_version"] != MIGRATION_HISTORY_SCHEMA
        or document["status"] != "passed"
        or document["source_revision"] != source_revision
        or not _documents_equal(workflow_run, {"id": run_id, "attempt": run_attempt})
        or not isinstance(cases, list)
    ):
        raise ValueError("historical migration evidence is not an exact successful run")
    for case in cases:
        _require_exact_keys(
            case, {"name", "return_code", "succeeded"}, label="historical migration case"
        )
    if (
        tuple(case["name"] for case in cases) != MIGRATION_CASES
        or any(
            type(case["return_code"]) is not int
            or case["return_code"] != 0
            or case["succeeded"] is not True
            for case in cases
        )
    ):
        raise ValueError("historical migration cases are incomplete or unsuccessful")
    return cases


def complete_migration_workload(
    historical: dict[str, Any],
    *,
    source_revision: str,
    run_id: int,
    run_attempt: int,
) -> dict[str, Any]:
    _require_sha(source_revision, label="source revision")
    _require_positive_int(run_id, label="run id")
    _require_positive_int(run_attempt, label="run attempt")
    cases = _validate_historical_migration(
        historical,
        source_revision=source_revision,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    return {
        "schema_version": MIGRATION_WORKLOAD_SCHEMA,
        "status": "passed",
        "source_revision": source_revision,
        "workflow_run": {"id": run_id, "attempt": run_attempt},
        "historical_cases": cases,
        "extended_safeguards": {
            "migrations_applied": True,
            "agent_storage_initialized": True,
            "main_extended_passed": True,
        },
    }


def _expected_recovery_token(run_id: int, run_attempt: int) -> str:
    return hashlib.sha256(f"{run_id}{run_attempt}".encode()).hexdigest()[:16]


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z") or not 20 <= len(value) <= 35:
        raise ValueError(f"{label} must be a bounded UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} must be a bounded UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError(f"{label} must be a bounded UTC timestamp")
    return parsed


def _validate_recovery_timeline(document: Any, *, elapsed_seconds: Any) -> None:
    sequence = (
        "started_at",
        "source_database_created_at",
        "migrations_completed_at",
        "agent_storage_initialized_at",
        "pre_backup_marker_at",
        "backup_started_at",
        "backup_restore_point_at",
        "backup_completed_at",
        "post_backup_marker_at",
        "source_loss_started_at",
        "source_loss_completed_at",
        "restore_started_at",
        "restore_completed_at",
        "validation_completed_at",
        "cleanup_completed_at",
        "completed_at",
    )
    timeline = _require_exact_keys(document, set(sequence), label="recovery timeline")
    timestamps = [_timestamp(timeline[key], label=f"recovery timeline {key}") for key in sequence]
    if timestamps != sorted(timestamps):
        raise ValueError("recovery timeline is not ordered")
    if type(elapsed_seconds) not in {int, float} or not 0 <= elapsed_seconds <= 1920:
        raise ValueError("recovery elapsed time is outside its bounded execution window")
    observed = (timestamps[-1] - timestamps[0]).total_seconds()
    if abs(float(elapsed_seconds) - observed) > 0.01:
        raise ValueError("recovery elapsed time does not match its timeline")


def _validate_recovery_measurements(document: Any) -> None:
    measurements = _require_exact_keys(
        document,
        {
            "backup_seconds",
            "recovery_point_gap_seconds",
            "recovery_point_gap_basis",
            "first_unrestored_write_age_seconds",
            "restore_to_validation_seconds",
            "source_loss_to_validation_seconds",
        },
        label="recovery measurements",
    )
    expected_basis = (
        "CockroachDB backup end_time restore point through the start of simulated source loss"
    )
    numeric_names = set(measurements) - {"recovery_point_gap_basis"}
    if measurements["recovery_point_gap_basis"] != expected_basis or any(
        type(measurements[name]) not in {int, float}
        or not 0 <= measurements[name] <= 1800
        for name in numeric_names
    ):
        raise ValueError("recovery measurements are malformed or outside their bounds")
    if measurements["restore_to_validation_seconds"] > measurements["source_loss_to_validation_seconds"]:
        raise ValueError("recovery measurement intervals are inconsistent")


def _validate_schema_summary(document: Any, *, label: str) -> None:
    summary = _require_exact_keys(
        document,
        {"sha256", "section_counts", "section_sha256"},
        label=label,
    )
    _require_sha256(summary["sha256"], label=f"{label} digest")
    counts = summary["section_counts"]
    digests = summary["section_sha256"]
    if (
        not isinstance(counts, dict)
        or not isinstance(digests, dict)
        or not counts
        or set(counts) != set(digests)
        or any(
            not isinstance(key, str)
            or not key
            or type(value) is not int
            or value < 0
            for key, value in counts.items()
        )
    ):
        raise ValueError(f"{label} sections are malformed")
    for digest in digests.values():
        _require_sha256(digest, label=f"{label} section digest")


def _validate_data_snapshot(document: Any, *, label: str) -> None:
    snapshot = _require_exact_keys(
        document,
        {"table_count", "row_count", "tables", "sha256"},
        label=label,
    )
    tables = snapshot["tables"]
    if (
        type(snapshot["table_count"]) is not int
        or snapshot["table_count"] < 1
        or type(snapshot["row_count"]) is not int
        or snapshot["row_count"] < 1
        or not isinstance(tables, dict)
        or len(tables) != snapshot["table_count"]
        or not tables
    ):
        raise ValueError(f"{label} counts are malformed")
    row_total = 0
    for table_name, table in tables.items():
        if not isinstance(table_name, str) or not table_name:
            raise ValueError(f"{label} contains an invalid table name")
        table = _require_exact_keys(
            table, {"columns", "row_count", "row_sha256"}, label=f"{label} table"
        )
        columns = table["columns"]
        if (
            not isinstance(columns, list)
            or not columns
            or any(not isinstance(column, str) or not column for column in columns)
            or len(set(columns)) != len(columns)
            or type(table["row_count"]) is not int
            or table["row_count"] < 0
        ):
            raise ValueError(f"{label} table shape is malformed")
        _require_sha256(table["row_sha256"], label=f"{label} row digest")
        row_total += table["row_count"]
    summary = {
        "table_count": snapshot["table_count"],
        "row_count": snapshot["row_count"],
        "tables": tables,
    }
    if row_total != snapshot["row_count"] or snapshot["sha256"] != _document_sha256(summary):
        raise ValueError(f"{label} digest or row total is invalid")


def _validate_recovery_validation(document: Any) -> None:
    validation = _require_exact_keys(
        document,
        {"markers", "schema_identity", "data_identity"},
        label="recovery validation",
    )
    if not _documents_equal(
        validation["markers"],
        {"pre_backup_present": True, "post_backup_absent": True},
    ):
        raise ValueError("recovery marker validation did not pass")
    schema_identity = _require_exact_keys(
        validation["schema_identity"],
        {"matches", "source", "restored", "differing_sections", "difference_sample"},
        label="recovery schema identity",
    )
    if (
        schema_identity["matches"] is not True
        or not _documents_equal(schema_identity["source"], schema_identity["restored"])
        or not _documents_equal(schema_identity["differing_sections"], [])
        or not _documents_equal(schema_identity["difference_sample"], {})
    ):
        raise ValueError("recovery schema identity did not pass exactly")
    _validate_schema_summary(schema_identity["source"], label="recovery schema summary")
    data_identity = _require_exact_keys(
        validation["data_identity"],
        {"matches", "source", "restored"},
        label="recovery data identity",
    )
    if data_identity["matches"] is not True or not _documents_equal(
        data_identity["source"], data_identity["restored"]
    ):
        raise ValueError("recovery data identity did not pass exactly")
    _validate_data_snapshot(data_identity["source"], label="recovery data snapshot")


def _expected_migration_summary(repository_root: Path, source_revision: str) -> dict[str, Any]:
    filenames = sorted(
        row["path"].removeprefix("migrations/")
        for row in _tree_rows(repository_root, source_revision)
        if row["path"].startswith("migrations/")
        and "/" not in row["path"].removeprefix("migrations/")
        and row["path"].endswith(".sql")
        and row["object_type"] == "blob"
    )
    if not filenames:
        raise ValueError("source revision has no migration inputs")
    return {
        "count": len(filenames),
        "last_filename": filenames[-1],
        "filenames_sha256": _document_sha256(filenames),
    }


def _validate_recovery_workload(
    document: dict[str, Any],
    *,
    repository_root: Path,
    source_revision: str,
    run_id: int,
    run_attempt: int,
) -> None:
    _require_exact_keys(
        document,
        {
            "schema_version",
            "status",
            "run_id",
            "source_sha",
            "scope",
            "timeline",
            "limitations",
            "engine",
            "topology",
            "migrations",
            "validation",
            "measurements",
            "cleanup",
            "elapsed_seconds",
        },
        label="recovery workload evidence",
    )
    token = _expected_recovery_token(run_id, run_attempt)
    expected_source = f"hindsight_recovery_source_{token}"
    expected_restore = f"hindsight_recovery_restore_{token}"
    expected_scope = {
        "source_database": expected_source,
        "restore_database": expected_restore,
        "backup_uri": f"userfile://defaultdb.public.hindsight_recovery_userfile_{token}/backup",
        "destructive_scope": "derived disposable resources only",
    }
    expected_cleanup = {
        "source_database_absent": True,
        "restore_database_absent": True,
        "userfile_tables_absent": True,
    }
    engine = _require_exact_keys(
        document["engine"],
        {"product", "version_string", "cluster_setting_version", "cluster_id"},
        label="recovery engine",
    )
    try:
        cluster_id_is_canonical = str(UUID(str(engine["cluster_id"]))) == engine["cluster_id"]
    except ValueError:
        cluster_id_is_canonical = False
    if (
        engine["product"] != "CockroachDB"
        or not isinstance(engine["version_string"], str)
        or not 1 <= len(engine["version_string"]) <= 1000
        or not isinstance(engine["cluster_setting_version"], str)
        or not 1 <= len(engine["cluster_setting_version"]) <= 100
        or not cluster_id_is_canonical
    ):
        raise ValueError("recovery engine identity is malformed")
    if (
        document["schema_version"] != RECOVERY_WORKLOAD_SCHEMA
        or document["status"] != "passed"
        or document["source_sha"] != source_revision
        or document["run_id"] != token
        or not _documents_equal(document["scope"], expected_scope)
        or not _documents_equal(document["limitations"], RECOVERY_LIMITATIONS)
        or not _documents_equal(
            document["topology"], {"mode": "local-single-node", "node_count": 1}
        )
        or not _documents_equal(
            document["migrations"],
            _expected_migration_summary(repository_root, source_revision),
        )
        or not _documents_equal(document["cleanup"], expected_cleanup)
    ):
        raise ValueError("recovery workload is not the exact successful bounded drill")
    _validate_recovery_timeline(document["timeline"], elapsed_seconds=document["elapsed_seconds"])
    _validate_recovery_measurements(document["measurements"])
    _validate_recovery_validation(document["validation"])


def _validate_workload(
    domain: str,
    document: dict[str, Any],
    *,
    repository_root: Path,
    source_revision: str,
    run_id: int,
    run_attempt: int,
) -> None:
    if domain == "migration":
        expected = complete_migration_workload(
            {
                "schema_version": MIGRATION_HISTORY_SCHEMA,
                "status": document.get("status"),
                "source_revision": document.get("source_revision"),
                "workflow_run": document.get("workflow_run"),
                "cases": document.get("historical_cases"),
            },
            source_revision=source_revision,
            run_id=run_id,
            run_attempt=run_attempt,
        )
        if not _documents_equal(document, expected):
            raise ValueError("migration workload evidence has unexpected fields or safeguards")
        return

    _validate_recovery_workload(
        document,
        repository_root=repository_root,
        source_revision=source_revision,
        run_id=run_id,
        run_attempt=run_attempt,
    )


def _validate_infrastructure_cleanup(
    document: dict[str, Any], *, source_revision: str
) -> None:
    _require_exact_keys(
        document,
        {
            "source_revision",
            "down_status",
            "container_query_status",
            "volume_query_status",
            "remaining_containers",
            "remaining_volumes",
            "compose_state_removed",
        },
        label="infrastructure cleanup evidence",
    )
    expected = {
        "source_revision": source_revision,
        "down_status": 0,
        "container_query_status": 0,
        "volume_query_status": 0,
        "remaining_containers": 0,
        "remaining_volumes": 0,
        "compose_state_removed": True,
    }
    if not _documents_equal(document, expected):
        raise ValueError("infrastructure cleanup was not verified")


def _workflow_identity(
    *,
    repository: str,
    workflow_path: str,
    source_revision: str,
    run_id: int,
    run_attempt: int,
    actor: str,
    triggering_actor: str,
    event_name: str,
    ref_name: str,
    workflow_ref: str,
) -> dict[str, Any]:
    owner = _repository_owner(repository)
    _require_sha(source_revision, label="source revision")
    _require_positive_int(run_id, label="run id")
    _require_positive_int(run_attempt, label="run attempt")
    expected_ref = f"{repository}/{workflow_path}@refs/heads/main"
    if (
        event_name != "workflow_dispatch"
        or ref_name != "refs/heads/main"
        or workflow_ref != expected_ref
        or actor != owner
        or triggering_actor != owner
    ):
        raise ValueError("workflow identity is not an owner dispatch on exact main")
    return {
        "repository": repository,
        "workflow_path": workflow_path,
        "workflow_ref": workflow_ref,
        "event": event_name,
        "ref": ref_name,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "actor": actor,
        "triggering_actor": triggering_actor,
    }


def _build_domain_receipt(
    domain: str,
    *,
    source_revision: str,
    workflow: dict[str, Any],
    manifest: dict[str, Any],
    files: Mapping[str, bytes],
) -> dict[str, Any]:
    spec = DOMAIN_SPECS[domain]
    base_names = (
        INPUT_MANIFEST_FILENAME,
        spec["workload_filename"],
        INFRASTRUCTURE_CLEANUP_FILENAME,
    )
    return {
        "schema_version": DOMAIN_RECEIPT_SCHEMA,
        "domain": domain,
        "status": "passed",
        "source_revision": source_revision,
        "workflow": workflow,
        "git_input_identity": {
            "inputs_sha256": manifest["inputs_sha256"],
            "entry_count": manifest["entry_count"],
            "manifest_sha256": manifest["manifest_sha256"],
        },
        "artifacts": {name: _file_record(files[name]) for name in base_names},
        "verification": {
            "workload_passed": True,
            "domain_cleanup_verified": True,
            "infrastructure_cleanup_verified": True,
        },
    }


def _build_artifact_manifest(
    domain: str,
    *,
    source_revision: str,
    workflow: dict[str, Any],
    files: Mapping[str, bytes],
) -> dict[str, Any]:
    return {
        "schema_version": ARTIFACT_MANIFEST_SCHEMA,
        "domain": domain,
        "source_revision": source_revision,
        "workflow_run": {
            "id": workflow["run_id"],
            "attempt": workflow["run_attempt"],
        },
        "files": {name: _file_record(value) for name, value in sorted(files.items())},
    }


def _bundle_names(domain: str) -> set[str]:
    spec = DOMAIN_SPECS[domain]
    return {
        INPUT_MANIFEST_FILENAME,
        spec["workload_filename"],
        INFRASTRUCTURE_CLEANUP_FILENAME,
        spec["receipt_filename"],
        ARTIFACT_MANIFEST_FILENAME,
    }


def validate_domain_bundle(
    domain: str,
    files: Mapping[str, bytes],
    *,
    repository_root: Path,
    source_revision: str,
    workflow: dict[str, Any],
) -> dict[str, Any]:
    if domain not in DOMAIN_SPECS:
        raise ValueError("unsupported evidence domain")
    if set(files) != _bundle_names(domain):
        raise ValueError("domain archive does not contain the exact required files")
    spec = DOMAIN_SPECS[domain]
    manifest = _load_json_bytes(files[INPUT_MANIFEST_FILENAME], label="Git input manifest")
    validate_git_manifest(
        manifest,
        repository_root=repository_root,
        revision=source_revision,
    )
    workload = _load_json_bytes(
        files[spec["workload_filename"]], label=f"{domain} workload evidence"
    )
    _validate_workload(
        domain,
        workload,
        repository_root=repository_root,
        source_revision=source_revision,
        run_id=workflow["run_id"],
        run_attempt=workflow["run_attempt"],
    )
    cleanup = _load_json_bytes(
        files[INFRASTRUCTURE_CLEANUP_FILENAME], label="infrastructure cleanup evidence"
    )
    _validate_infrastructure_cleanup(cleanup, source_revision=source_revision)

    base_files = {
        name: files[name]
        for name in (
            INPUT_MANIFEST_FILENAME,
            spec["workload_filename"],
            INFRASTRUCTURE_CLEANUP_FILENAME,
        )
    }
    expected_receipt = _build_domain_receipt(
        domain,
        source_revision=source_revision,
        workflow=workflow,
        manifest=manifest,
        files=base_files,
    )
    receipt = _load_json_bytes(files[spec["receipt_filename"]], label=f"{domain} receipt")
    if not _documents_equal(receipt, expected_receipt):
        raise ValueError(f"{domain} receipt does not bind the exact validated evidence")

    manifest_files = {**base_files, spec["receipt_filename"]: files[spec["receipt_filename"]]}
    expected_artifact_manifest = _build_artifact_manifest(
        domain,
        source_revision=source_revision,
        workflow=workflow,
        files=manifest_files,
    )
    artifact_manifest = _load_json_bytes(
        files[ARTIFACT_MANIFEST_FILENAME], label="artifact manifest"
    )
    if not _documents_equal(artifact_manifest, expected_artifact_manifest):
        raise ValueError("artifact manifest does not bind the exact domain files")
    return receipt


def finalize_domain_evidence(
    domain: str,
    *,
    evidence_dir: Path,
    repository_root: Path,
    source_revision: str,
    workflow: dict[str, Any],
) -> dict[str, Any]:
    if domain not in DOMAIN_SPECS:
        raise ValueError("unsupported evidence domain")
    spec = DOMAIN_SPECS[domain]
    initial_paths = list(evidence_dir.iterdir()) if evidence_dir.exists() else []
    initial_names = {path.name for path in initial_paths}
    expected_initial = {
        INPUT_MANIFEST_FILENAME,
        spec["workload_filename"],
        INFRASTRUCTURE_CLEANUP_FILENAME,
    }
    if (
        initial_names != expected_initial
        or len(initial_paths) != len(expected_initial)
        or any(not path.is_file() or path.is_symlink() for path in initial_paths)
    ):
        raise ValueError("evidence directory does not contain the exact pre-receipt files")
    base_files = {name: (evidence_dir / name).read_bytes() for name in expected_initial}
    manifest = _load_json_bytes(base_files[INPUT_MANIFEST_FILENAME], label="Git input manifest")
    validate_git_manifest(
        manifest,
        repository_root=repository_root,
        revision=source_revision,
    )
    workload = _load_json_bytes(
        base_files[spec["workload_filename"]], label=f"{domain} workload evidence"
    )
    _validate_workload(
        domain,
        workload,
        repository_root=repository_root,
        source_revision=source_revision,
        run_id=workflow["run_id"],
        run_attempt=workflow["run_attempt"],
    )
    cleanup = _load_json_bytes(
        base_files[INFRASTRUCTURE_CLEANUP_FILENAME], label="infrastructure cleanup evidence"
    )
    _validate_infrastructure_cleanup(cleanup, source_revision=source_revision)

    receipt = _build_domain_receipt(
        domain,
        source_revision=source_revision,
        workflow=workflow,
        manifest=manifest,
        files=base_files,
    )
    receipt_path = evidence_dir / spec["receipt_filename"]
    _write_json(receipt_path, receipt)
    manifest_files = {**base_files, spec["receipt_filename"]: receipt_path.read_bytes()}
    artifact_manifest = _build_artifact_manifest(
        domain,
        source_revision=source_revision,
        workflow=workflow,
        files=manifest_files,
    )
    _write_json(evidence_dir / ARTIFACT_MANIFEST_FILENAME, artifact_manifest)
    final_paths = list(evidence_dir.iterdir())
    if (
        {path.name for path in final_paths} != _bundle_names(domain)
        or len(final_paths) != len(_bundle_names(domain))
        or any(not path.is_file() or path.is_symlink() for path in final_paths)
    ):
        raise ValueError("evidence directory does not contain the exact finalized bundle")
    complete_files = {
        name: (evidence_dir / name).read_bytes() for name in _bundle_names(domain)
    }
    validate_domain_bundle(
        domain,
        complete_files,
        repository_root=repository_root,
        source_revision=source_revision,
        workflow=workflow,
    )
    return receipt


def verify_source_run(
    payload: dict[str, Any],
    *,
    domain: str,
    repository: str,
    run_id: int,
    run_attempt: int,
) -> tuple[str, dict[str, Any], str]:
    if domain not in DOMAIN_SPECS:
        raise ValueError("unsupported evidence domain")
    owner = _repository_owner(repository)
    _require_positive_int(run_id, label="source run id")
    _require_positive_int(run_attempt, label="source run attempt")
    source_revision = _require_sha(payload.get("head_sha"), label="source run head SHA")
    actor = payload.get("actor")
    triggering_actor = payload.get("triggering_actor")
    run_repository = payload.get("repository")
    html_url = payload.get("html_url")
    expected_path = DOMAIN_SPECS[domain]["workflow_path"]
    if (
        type(payload.get("id")) is not int
        or payload.get("id") != run_id
        or type(payload.get("run_attempt")) is not int
        or payload.get("run_attempt") != run_attempt
        or payload.get("status") != "completed"
        or payload.get("conclusion") != "success"
        or payload.get("event") != "workflow_dispatch"
        or payload.get("head_branch") != "main"
        or payload.get("path") != expected_path
        or not isinstance(actor, dict)
        or actor.get("login") != owner
        or not isinstance(triggering_actor, dict)
        or triggering_actor.get("login") != owner
        or not isinstance(run_repository, dict)
        or run_repository.get("full_name") != repository
        or not isinstance(html_url, str)
        or html_url != f"https://github.com/{repository}/actions/runs/{run_id}"
    ):
        raise ValueError("source workflow metadata is not the exact successful owner main run")
    workflow = _workflow_identity(
        repository=repository,
        workflow_path=expected_path,
        source_revision=source_revision,
        run_id=run_id,
        run_attempt=run_attempt,
        actor=owner,
        triggering_actor=owner,
        event_name="workflow_dispatch",
        ref_name="refs/heads/main",
        workflow_ref=f"{repository}/{expected_path}@refs/heads/main",
    )
    return source_revision, workflow, html_url


def verify_source_artifact(
    payload: dict[str, Any],
    archive: bytes,
    *,
    domain: str,
    source_revision: str,
    run_id: int,
    run_attempt: int,
) -> dict[str, Any]:
    artifacts = payload.get("artifacts")
    if (
        not isinstance(artifacts, list)
        or type(payload.get("total_count")) is not int
        or payload["total_count"] != len(artifacts)
    ):
        raise ValueError("source artifact metadata is incomplete or paginated")
    name = f"{DOMAIN_SPECS[domain]['artifact_prefix']}-{run_id}-{run_attempt}"
    matches = [artifact for artifact in artifacts if isinstance(artifact, dict) and artifact.get("name") == name]
    if len(matches) != 1:
        raise ValueError("exact source artifact identity is absent or ambiguous")
    artifact = matches[0]
    workflow_run = artifact.get("workflow_run")
    digest = artifact.get("digest")
    expected_digest = (
        digest.removeprefix("sha256:")
        if isinstance(digest, str) and digest.startswith("sha256:")
        else ""
    )
    artifact_id = artifact.get("id")
    size_bytes = artifact.get("size_in_bytes")
    if (
        type(artifact_id) is not int
        or artifact_id < 1
        or type(size_bytes) is not int
        or size_bytes < 1
        or size_bytes != len(archive)
        or artifact.get("expired") is not False
        or SHA256_PATTERN.fullmatch(expected_digest) is None
        or not isinstance(workflow_run, dict)
        or type(workflow_run.get("id")) is not int
        or workflow_run.get("id") != run_id
        or workflow_run.get("head_branch") != "main"
        or workflow_run.get("head_sha") != source_revision
    ):
        raise ValueError("source artifact metadata does not match the exact source run")
    archive_digest = _bytes_sha256(archive)
    if archive_digest != expected_digest:
        raise ValueError("downloaded source artifact digest does not match GitHub metadata")
    return {
        "id": artifact_id,
        "name": name,
        "digest": digest,
        "size_bytes": size_bytes,
        "archive_sha256": archive_digest,
    }


def _archive_files(archive: bytes, *, domain: str) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            files: dict[str, bytes] = {}
            for info in bundle.infolist():
                if info.is_dir():
                    raise ValueError("source artifact contains an unexpected directory entry")
                path = PurePosixPath(info.filename)
                file_type = (info.external_attr >> 16) & 0o170000
                if (
                    path.is_absolute()
                    or len(path.parts) != 1
                    or path.name in files
                    or file_type not in {0, 0o100000}
                ):
                    raise ValueError("source artifact contains an unsafe or duplicate entry")
                files[path.name] = bundle.read(info)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise ValueError("source artifact is not a valid ZIP archive") from exc
    if set(files) != _bundle_names(domain):
        raise ValueError("source artifact ZIP does not contain the exact domain bundle")
    return files


def create_reuse_evidence(
    *,
    repository_root: Path,
    repository: str,
    target_revision: str,
    target_workflow: dict[str, Any],
    sources: Mapping[str, Mapping[str, Any]],
    output_dir: Path,
) -> dict[str, Any]:
    target_revision = _resolve_commit(repository_root, target_revision)
    target_manifest = build_git_manifest(repository_root, target_revision)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError("reuse output directory must be empty")
    target_manifest_path = output_dir / TARGET_MANIFEST_FILENAME
    _write_json(target_manifest_path, target_manifest)

    source_receipts: dict[str, Any] = {}
    if set(sources) != set(DOMAIN_SPECS):
        raise ValueError("reuse requires exactly migration and recovery sources")
    for domain in ("migration", "recovery"):
        source = sources[domain]
        run_id = _require_positive_int(source.get("run_id"), label=f"{domain} run id")
        run_attempt = _require_positive_int(
            source.get("run_attempt"), label=f"{domain} run attempt"
        )
        run_payload = source.get("run_metadata")
        artifact_payload = source.get("artifact_metadata")
        archive = source.get("archive")
        if not isinstance(run_payload, dict) or not isinstance(artifact_payload, dict):
            raise ValueError(f"{domain} source metadata must contain JSON objects")
        if not isinstance(archive, bytes):
            raise ValueError(f"{domain} source archive must contain bytes")
        source_revision, source_workflow, run_url = verify_source_run(
            run_payload,
            domain=domain,
            repository=repository,
            run_id=run_id,
            run_attempt=run_attempt,
        )
        artifact = verify_source_artifact(
            artifact_payload,
            archive,
            domain=domain,
            source_revision=source_revision,
            run_id=run_id,
            run_attempt=run_attempt,
        )
        files = _archive_files(archive, domain=domain)
        receipt = validate_domain_bundle(
            domain,
            files,
            repository_root=repository_root,
            source_revision=source_revision,
            workflow=source_workflow,
        )
        source_manifest = _load_json_bytes(
            files[INPUT_MANIFEST_FILENAME], label=f"{domain} Git input manifest"
        )
        if (
            source_manifest["inputs_sha256"] != target_manifest["inputs_sha256"]
            or not _documents_equal(source_manifest["entries"], target_manifest["entries"])
        ):
            raise ValueError(f"{domain} non-document inputs differ from the exact target")
        documentation_delta = verify_documentation_delta(
            repository_root,
            source_revision=source_revision,
            target_revision=target_revision,
        )
        receipt_name = DOMAIN_SPECS[domain]["receipt_filename"]
        source_receipts[domain] = {
            "source_revision": source_revision,
            "workflow": {
                **source_workflow,
                "status": "completed",
                "conclusion": "success",
                "head_branch": "main",
                "head_sha": source_revision,
                "html_url": run_url,
            },
            "artifact": artifact,
            "domain_receipt": {
                "schema_version": receipt["schema_version"],
                "sha256": _bytes_sha256(files[receipt_name]),
            },
            "git_input_manifest": {
                "inputs_sha256": source_manifest["inputs_sha256"],
                "manifest_sha256": source_manifest["manifest_sha256"],
                "artifact_sha256": _bytes_sha256(files[INPUT_MANIFEST_FILENAME]),
            },
            "documentation_delta": documentation_delta,
        }

    receipt = {
        "schema_version": REUSE_RECEIPT_SCHEMA,
        "status": "passed",
        "target_revision": target_revision,
        "target_workflow": target_workflow,
        "target_git_input_manifest": {
            "inputs_sha256": target_manifest["inputs_sha256"],
            "manifest_sha256": target_manifest["manifest_sha256"],
            "artifact_sha256": _bytes_sha256(target_manifest_path.read_bytes()),
        },
        "sources": source_receipts,
        "execution": {
            "migration_rerun": False,
            "recovery_rerun": False,
            "database_started": False,
        },
    }
    receipt_path = output_dir / REUSE_RECEIPT_FILENAME
    _write_json(receipt_path, receipt)
    checksum = _bytes_sha256(receipt_path.read_bytes())
    (output_dir / REUSE_CHECKSUM_FILENAME).write_text(
        f"{checksum}  {REUSE_RECEIPT_FILENAME}\n", encoding="ascii"
    )
    return receipt


def _workflow_from_args(args: argparse.Namespace, *, workflow_path: str) -> dict[str, Any]:
    return _workflow_identity(
        repository=args.repository,
        workflow_path=workflow_path,
        source_revision=args.source_revision,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        actor=args.actor,
        triggering_actor=args.triggering_actor,
        event_name=args.event_name,
        ref_name=args.ref_name,
        workflow_ref=args.workflow_ref,
    )


def _add_workflow_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repository", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--triggering-actor", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--ref-name", required=True)
    parser.add_argument("--workflow-ref", required=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    manifest_parser.add_argument("--revision", required=True)
    manifest_parser.add_argument("--output", type=Path, required=True)

    migration_parser = subparsers.add_parser("complete-migration")
    migration_parser.add_argument("--historical", type=Path, required=True)
    migration_parser.add_argument("--source-revision", required=True)
    migration_parser.add_argument("--run-id", required=True, type=int)
    migration_parser.add_argument("--run-attempt", required=True, type=int)
    migration_parser.add_argument("--output", type=Path, required=True)

    finalize_parser = subparsers.add_parser("finalize-domain")
    finalize_parser.add_argument("--domain", choices=tuple(DOMAIN_SPECS), required=True)
    finalize_parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    finalize_parser.add_argument("--evidence-dir", type=Path, required=True)
    _add_workflow_arguments(finalize_parser)

    reuse_parser = subparsers.add_parser("reuse")
    reuse_parser.add_argument("--repository-root", type=Path, default=Path.cwd())
    reuse_parser.add_argument("--repository", required=True)
    reuse_parser.add_argument("--target-revision", required=True)
    reuse_parser.add_argument("--run-id", required=True, type=int)
    reuse_parser.add_argument("--run-attempt", required=True, type=int)
    reuse_parser.add_argument("--actor", required=True)
    reuse_parser.add_argument("--triggering-actor", required=True)
    reuse_parser.add_argument("--event-name", required=True)
    reuse_parser.add_argument("--ref-name", required=True)
    reuse_parser.add_argument("--workflow-ref", required=True)
    for domain in DOMAIN_SPECS:
        reuse_parser.add_argument(f"--{domain}-run-id", required=True, type=int)
        reuse_parser.add_argument(f"--{domain}-run-attempt", required=True, type=int)
        reuse_parser.add_argument(f"--{domain}-run-metadata", required=True, type=Path)
        reuse_parser.add_argument(f"--{domain}-artifact-metadata", required=True, type=Path)
        reuse_parser.add_argument(f"--{domain}-archive", required=True, type=Path)
    reuse_parser.add_argument("--output-dir", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "manifest":
        write_git_manifest(args.repository_root, args.revision, args.output)
    elif args.command == "complete-migration":
        workload = complete_migration_workload(
            _load_json(args.historical, label="historical migration evidence"),
            source_revision=args.source_revision,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
        )
        _write_json(args.output, workload)
    elif args.command == "finalize-domain":
        spec = DOMAIN_SPECS[args.domain]
        workflow = _workflow_from_args(args, workflow_path=spec["workflow_path"])
        finalize_domain_evidence(
            args.domain,
            evidence_dir=args.evidence_dir,
            repository_root=args.repository_root,
            source_revision=args.source_revision,
            workflow=workflow,
        )
    else:
        workflow = _workflow_identity(
            repository=args.repository,
            workflow_path=".github/workflows/evidence-reuse.yml",
            source_revision=args.target_revision,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            actor=args.actor,
            triggering_actor=args.triggering_actor,
            event_name=args.event_name,
            ref_name=args.ref_name,
            workflow_ref=args.workflow_ref,
        )
        sources = {
            domain: {
                "run_id": getattr(args, f"{domain}_run_id"),
                "run_attempt": getattr(args, f"{domain}_run_attempt"),
                "run_metadata": _load_json(
                    getattr(args, f"{domain}_run_metadata"),
                    label=f"{domain} run metadata",
                ),
                "artifact_metadata": _load_json(
                    getattr(args, f"{domain}_artifact_metadata"),
                    label=f"{domain} artifact metadata",
                ),
                "archive": getattr(args, f"{domain}_archive").read_bytes(),
            }
            for domain in DOMAIN_SPECS
        }
        create_reuse_evidence(
            repository_root=args.repository_root,
            repository=args.repository,
            target_revision=args.target_revision,
            target_workflow=workflow,
            sources=sources,
            output_dir=args.output_dir,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
