"""Fail closed unless capacity evidence matches the bounded qualification protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

TARGETS = {"vectors": 100_000, "tenants": 20, "clients": 20, "backlog_messages": 1_000}
EXPECTED_CEILINGS = {
    "duration_seconds": 1_200,
    "storage_bytes": 1_500_000_000,
    "clients": 20,
    "external_cost_usd": 0,
}
EXPECTED_INDEX = "semantic_memory_vectors_tenant_namespace_profile_embedding_idx"
LEGACY_INDEX = "semantic_memory_vectors_embedding_idx"
EXPECTED_INDEXES = sorted((LEGACY_INDEX, EXPECTED_INDEX))
BASE_SCHEMA_THROUGH = "0029e_product_credential_locators.sql"
LEGACY_VECTOR_MIGRATION = "0009_embedding_profiles_and_retrieval.sql"
TENANT_VECTOR_MIGRATION = "0030_tenant_vector_cosine_index.sql"
EXPECTED_VECTOR_INSERT_WORKERS = 1
EXPECTED_VECTOR_METHOD = "deterministic_tenant_anchored_13bit_1024d"
EXPECTED_SEEDING_METHOD = (
    "single_bounded_writer_twenty_atomic_per_tenant_copy_transactions_"
    "between_exact_legacy_index_drop_and_restore"
)
EXPECTED_FIXTURE_VECTOR_INDEXES = (
    "legacy_only_before_seed_then_none_during_copy_then_legacy_restored_"
    "before_populated_tenant_index_migration"
)
EXPECTED_DATABASE_METHOD = "disposable_local_single_node_cockroachdb_in_memory_8_gib"
SCHEMA_VERSION = "hindsight.capacity_qualification.v3"
DATABASE_PATTERN = re.compile(r"hindsight_capacity_[a-z0-9]{8,20}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _matches_exact_integers(value: Any, expected: dict[str, int]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(expected)
        and all(type(value[key]) is int and value[key] == expected[key] for key in expected)
    )


def _is_canonical_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _measurement_map(rows: list[Any]) -> dict[str, dict[str, Any]]:
    if any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in rows):
        raise ValueError("capacity raw measurements must be named objects")
    values = {row["name"]: row for row in rows}
    if len(values) != len(rows):
        raise ValueError("capacity raw measurement names must be unique")
    return values


def _validate_measurements(rows: list[Any]) -> None:
    expected_names = [
        "base_migrations",
        "legacy_index_suspension",
        "vector_seed",
        "legacy_index_restore",
        "tenant_index_build_input",
        "post_seed_migrations",
        "vector_indexes",
        "vector_counts",
        "bounded_clients",
        "synthetic_backlog",
        "storage",
        "total",
    ]
    if [row.get("name") if isinstance(row, dict) else None for row in rows] != expected_names:
        raise ValueError("capacity evidence does not preserve the required measurement sequence")
    values = _measurement_map(rows)
    if set(values) != set(expected_names):
        raise ValueError("capacity evidence does not contain the complete measurement set")
    base_migrations = values["base_migrations"]
    legacy_index_suspension = values["legacy_index_suspension"]
    legacy_index_restore = values["legacy_index_restore"]
    post_seed_migrations = values["post_seed_migrations"]
    index_build_input = values["tenant_index_build_input"]
    vector_indexes = values["vector_indexes"]
    timed_rows = (
        base_migrations,
        legacy_index_suspension,
        values["vector_seed"],
        legacy_index_restore,
        post_seed_migrations,
        values["synthetic_backlog"],
    )
    if any(
        type(row.get("duration_seconds")) not in {int, float}
        or not 0 < row["duration_seconds"] <= EXPECTED_CEILINGS["duration_seconds"]
        for row in timed_rows
    ):
        raise ValueError("capacity phase durations exceed or omit the duration ceiling")
    if (
        base_migrations.get("through") != BASE_SCHEMA_THROUGH
        or post_seed_migrations.get("through") != "latest"
        or legacy_index_suspension.get("removed_index") != LEGACY_INDEX
        or legacy_index_suspension.get("before_indexes") != [LEGACY_INDEX]
        or legacy_index_suspension.get("after_indexes") != []
        or legacy_index_restore.get("migration") != LEGACY_VECTOR_MIGRATION
        or legacy_index_restore.get("restored_index") != LEGACY_INDEX
        or legacy_index_restore.get("before_indexes") != []
        or legacy_index_restore.get("after_indexes") != [LEGACY_INDEX]
        or type(legacy_index_restore.get("vectors")) is not int
        or legacy_index_restore["vectors"] != TARGETS["vectors"]
        or type(legacy_index_restore.get("storage_bytes")) is not int
        or not 0
        < legacy_index_restore["storage_bytes"]
        <= EXPECTED_CEILINGS["storage_bytes"]
        or type(index_build_input.get("vectors")) is not int
        or index_build_input["vectors"] != TARGETS["vectors"]
        or index_build_input.get("present_indexes") != [LEGACY_INDEX]
        or index_build_input.get("absent_index") != EXPECTED_INDEX
        or index_build_input.get("next_migration") != TENANT_VECTOR_MIGRATION
        or vector_indexes.get("indexes") != EXPECTED_INDEXES
        or type(vector_indexes.get("storage_bytes")) is not int
        or not 0 < vector_indexes["storage_bytes"] <= EXPECTED_CEILINGS["storage_bytes"]
    ):
        raise ValueError(
            "capacity evidence does not prove a populated tenant-index build with both indexes live"
        )
    seed = values["vector_seed"]
    storage_checks = seed.get("storage_checks")
    if (
        type(seed.get("batches")) is not int
        or seed["batches"] != TARGETS["tenants"]
        or type(seed.get("vector_insert_rows")) is not int
        or seed["vector_insert_rows"] != TARGETS["vectors"]
        or type(seed.get("vector_insert_transactions")) is not int
        or seed["vector_insert_transactions"] != TARGETS["tenants"]
        or type(seed.get("vector_insert_workers")) is not int
        or seed["vector_insert_workers"] != EXPECTED_VECTOR_INSERT_WORKERS
        or type(seed.get("vector_insert_client_retries")) is not int
        or seed["vector_insert_client_retries"] != 0
    ):
        raise ValueError("capacity seeding does not prove exact bounded vector insertion")
    if (
        not isinstance(storage_checks, list)
        or len(storage_checks) != TARGETS["tenants"]
        or [row.get("completion_sequence") for row in storage_checks if isinstance(row, dict)]
        != list(range(1, TARGETS["tenants"] + 1))
        or any(
            not isinstance(row, dict)
            or type(row.get("completion_sequence")) is not int
            or type(row.get("completed_tenants")) is not int
            or row["completed_tenants"] != row["completion_sequence"]
            or type(row.get("bytes")) is not int
            or not 0 < row["bytes"] <= EXPECTED_CEILINGS["storage_bytes"]
            for row in storage_checks
        )
        or type(seed.get("peak_storage_bytes")) is not int
        or seed["peak_storage_bytes"] != max(row["bytes"] for row in storage_checks)
    ):
        raise ValueError("capacity seeding does not prove the enforced storage ceiling")
    counts = values["vector_counts"]
    per_tenant = counts.get("per_tenant")
    tenant_ids = (
        [row.get("tenant_id") for row in per_tenant if isinstance(row, dict)]
        if isinstance(per_tenant, list)
        else []
    )
    if (
        type(counts.get("total")) is not int
        or counts["total"] != TARGETS["vectors"]
        or not isinstance(per_tenant, list)
        or len(per_tenant) != TARGETS["tenants"]
        or len(set(tenant_ids)) != TARGETS["tenants"]
        or any(
            not isinstance(row, dict)
            or not _is_canonical_uuid(row.get("tenant_id"))
            or type(row.get("vectors")) is not int
            or row["vectors"] != 5_000
            for row in per_tenant
        )
    ):
        raise ValueError("capacity measurements do not prove exact vector and tenant counts")
    clients = values["bounded_clients"].get("clients")
    if (
        not isinstance(clients, list)
        or len(clients) != TARGETS["clients"]
        or {row.get("client") for row in clients if isinstance(row, dict)} != set(range(1, 21))
        or [row.get("tenant_id") for row in clients if isinstance(row, dict)] != tenant_ids
        or any(
            not isinstance(row, dict)
            or type(row.get("client")) is not int
            or row.get("qualified_index") != EXPECTED_INDEX
            or "vector search" not in str(row.get("plan", "")).lower()
            or f"@{EXPECTED_INDEX}" not in str(row.get("plan", ""))
            or not row.get("prefix_spans")
            for row in clients
        )
    ):
        raise ValueError("capacity measurements do not prove twenty qualified clients")
    backlog = values["synthetic_backlog"]
    per_client = backlog.get("per_client_counts")
    if (
        not _matches_exact_integers(
            {
                key: backlog.get(key)
                for key in (
                    "messages_enqueued",
                    "messages_drained",
                    "messages_accounted_for",
                    "queue_capacity",
                    "pending_before_drain",
                    "pending_after_drain",
                    "observed_max_pending",
                    "clients",
                    "live_worker_invocations",
                    "paid_model_calls",
                )
            },
            {
                "messages_enqueued": TARGETS["backlog_messages"],
                "messages_drained": TARGETS["backlog_messages"],
                "messages_accounted_for": TARGETS["backlog_messages"],
                "queue_capacity": TARGETS["backlog_messages"],
                "pending_before_drain": TARGETS["backlog_messages"],
                "pending_after_drain": 0,
                "observed_max_pending": TARGETS["backlog_messages"],
                "clients": TARGETS["clients"],
                "live_worker_invocations": 0,
                "paid_model_calls": 0,
            },
        )
        or not isinstance(per_client, list)
        or len(per_client) != TARGETS["clients"]
        or any(type(count) is not int or count <= 0 for count in per_client)
        or sum(per_client) != TARGETS["backlog_messages"]
    ):
        raise ValueError("capacity measurements do not prove the isolated synthetic backlog")
    storage = values["storage"].get("bytes")
    duration = values["total"].get("duration_seconds")
    if (
        type(storage) is not int
        or not 0 < storage <= EXPECTED_CEILINGS["storage_bytes"]
        or storage < seed["peak_storage_bytes"]
    ):
        raise ValueError("capacity measurements exceed or omit the storage ceiling")
    if (
        type(duration) not in {int, float}
        or not 0 < duration <= EXPECTED_CEILINGS["duration_seconds"]
    ):
        raise ValueError("capacity measurements exceed or omit the duration ceiling")
    if sum(row["duration_seconds"] for row in timed_rows) > duration + 0.01:
        raise ValueError("capacity phase durations exceed the measured total duration")


def validate(
    document: dict[str, Any],
    *,
    source_revision: str,
    qualification: dict[str, Any] | None = None,
    cleanup: dict[str, Any] | None = None,
    artifact_digests: dict[str, str] | None = None,
) -> dict[str, Any]:
    if qualification is None or cleanup is None or artifact_digests is None:
        raise ValueError(
            "capacity validation requires qualification, cleanup, and artifact manifest evidence"
        )
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    qualification_link = document.get("index_qualification") or {}
    cleanup_link = document.get("cleanup") or {}
    if qualification_link.get("qualified") is not True:
        raise ValueError("capacity evidence requires a qualified populated vector index")
    if re.fullmatch(r"[0-9a-f]{64}", str(qualification_link.get("artifact_sha256") or "")) is None:
        raise ValueError("capacity evidence requires a full SHA-256 qualification artifact digest")
    if (
        cleanup_link.get("database_removed") is not True
        or re.fullmatch(r"[0-9a-f]{64}", str(cleanup_link.get("artifact_sha256") or ""))
        is None
    ):
        raise ValueError("capacity evidence requires a bound cleanup artifact")
    if qualification_link.get("main_sha") != source_revision:
        raise ValueError("index qualification must belong to the exact tested main revision")
    if document.get("source_revision") != source_revision:
        raise ValueError("capacity evidence must belong to the exact tested main revision")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("capacity evidence schema version is unsupported")
    if not _matches_exact_integers(document.get("targets"), TARGETS):
        raise ValueError("capacity evidence does not match the bounded target shape")
    if not _matches_exact_integers(document.get("ceilings"), EXPECTED_CEILINGS):
        raise ValueError("capacity evidence does not enforce the required hard ceilings")
    method = document.get("method")
    if not isinstance(method, dict) or not method or not document.get("environment"):
        raise ValueError("capacity evidence requires method and environment")
    if method.get("database") != EXPECTED_DATABASE_METHOD:
        raise ValueError("capacity evidence does not identify the isolated database method")
    if method.get("vectors") != EXPECTED_VECTOR_METHOD:
        raise ValueError("capacity evidence does not identify the deterministic vector fixture")
    if method.get("seeding") != EXPECTED_SEEDING_METHOD:
        raise ValueError("capacity evidence does not identify the bounded vector seeding method")
    if method.get("fixture_vector_indexes") != EXPECTED_FIXTURE_VECTOR_INDEXES:
        raise ValueError("capacity evidence does not identify the bounded index lifecycle")
    environment = document["environment"]
    if (
        not isinstance(environment, dict)
        or type(environment.get("paid_model_calls")) is not int
        or environment["paid_model_calls"] != 0
        or type(environment.get("live_worker_invocations")) is not int
        or environment["live_worker_invocations"] != 0
        or environment.get("isolation") != "run_scoped_database_and_compose_project"
    ):
        raise ValueError("capacity environment is not isolated from paid and live services")
    measurements = document.get("raw_measurements")
    if not isinstance(measurements, list) or not measurements:
        raise ValueError("capacity evidence requires raw measurements")
    _validate_measurements(measurements)
    limitations = document.get("limitations")
    if not isinstance(limitations, list) or not limitations:
        raise ValueError("capacity evidence requires explicit limitations")
    if "not production SLO claims" not in " ".join(map(str, limitations)):
        raise ValueError("capacity evidence must reject production SLO interpretation")
    if (
        qualification.get("schema_version") != SCHEMA_VERSION
        or qualification.get("qualified") is not True
        or qualification.get("main_sha") != source_revision
        or qualification.get("index") != EXPECTED_INDEX
        or qualification.get("indexes") != EXPECTED_INDEXES
        or type(qualification.get("vector_dimensions")) is not int
        or qualification["vector_dimensions"] != 1024
        or type(qualification.get("vector_count")) is not int
        or qualification["vector_count"] != TARGETS["vectors"]
        or type(qualification.get("tenant_count")) is not int
        or qualification["tenant_count"] != TARGETS["tenants"]
    ):
        raise ValueError("index qualification does not prove the exact populated target")
    counts = qualification.get("per_tenant_counts")
    qualification_tenant_ids = (
        [row.get("tenant_id") for row in counts if isinstance(row, dict)]
        if isinstance(counts, list)
        else []
    )
    if (
        not isinstance(counts, list)
        or len(counts) != TARGETS["tenants"]
        or len(set(qualification_tenant_ids)) != TARGETS["tenants"]
        or any(
            not isinstance(row, dict)
            or not _is_canonical_uuid(row.get("tenant_id"))
            or type(row.get("vectors")) is not int
            or row["vectors"] != 5_000
            for row in counts
        )
    ):
        raise ValueError("index qualification has invalid per-tenant counts")
    plans = qualification.get("plans")
    if (
        not isinstance(plans, list)
        or len(plans) != TARGETS["clients"]
        or {row.get("client") for row in plans if isinstance(row, dict)} != set(range(1, 21))
        or [row.get("tenant_id") for row in plans if isinstance(row, dict)]
        != qualification_tenant_ids
        or any(
            not isinstance(row, dict)
            or type(row.get("client")) is not int
            or row.get("qualified_index") != EXPECTED_INDEX
            for row in plans
        )
    ):
        raise ValueError("index qualification does not prove twenty bounded clients")
    measured = _measurement_map(measurements)
    if counts != measured["vector_counts"].get("per_tenant"):
        raise ValueError("capacity report vector counts differ from index qualification")
    if plans != measured["bounded_clients"].get("clients"):
        raise ValueError("capacity report client plans differ from index qualification")
    if (
        cleanup.get("schema_version") != SCHEMA_VERSION
        or DATABASE_PATTERN.fullmatch(str(cleanup.get("database", ""))) is None
        or cleanup.get("database_removed") is not True
        or "error" not in cleanup
        or cleanup.get("error") is not None
        or cleanup.get("source_revision") != source_revision
        or type(cleanup.get("timeout_seconds")) is not int
        or cleanup["timeout_seconds"] != 120
    ):
        raise ValueError("capacity evidence requires verified disposable-state cleanup")
    expected_names = {"index-qualification.json", "capacity-report.json", "cleanup.json"}
    if (
        set(artifact_digests) != expected_names
        or any(
            re.fullmatch(r"[0-9a-f]{64}", str(digest)) is None
            for digest in artifact_digests.values()
        )
        or artifact_digests["index-qualification.json"]
        != qualification_link["artifact_sha256"]
        or artifact_digests["cleanup.json"] != cleanup_link["artifact_sha256"]
    ):
        raise ValueError("capacity artifact manifest is incomplete")
    return {
        **document,
        "kind": "bounded_capacity_evidence",
        "claim_scope": "benchmark_evidence_not_production_slo",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--cleanup", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.input.read_text())
    qualification = json.loads(args.qualification.read_text())
    cleanup = json.loads(args.cleanup.read_text())
    manifest = json.loads(args.manifest.read_text())
    digests = manifest.get("artifacts") if isinstance(manifest, dict) else None
    actual = {
        "index-qualification.json": _sha256(args.qualification),
        "capacity-report.json": _sha256(args.input),
        "cleanup.json": _sha256(args.cleanup),
    }
    if (
        digests != actual
        or manifest.get("source_revision") != args.source_revision
        or manifest.get("schema_version") != SCHEMA_VERSION
    ):
        raise ValueError("capacity artifact hashes do not match the supplied files")
    if (
        document.get("index_qualification", {}).get("artifact_sha256")
        != actual["index-qualification.json"]
    ):
        raise ValueError("capacity report does not bind the qualification artifact")
    if document.get("cleanup", {}).get("artifact_sha256") != actual["cleanup.json"]:
        raise ValueError("capacity report does not bind the cleanup artifact")
    report = validate(
        document,
        source_revision=args.source_revision,
        qualification=qualification,
        cleanup=cleanup,
        artifact_digests=digests,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
