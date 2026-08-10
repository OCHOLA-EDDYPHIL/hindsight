"""Fail closed unless capacity evidence matches the bounded qualification protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

TARGETS = {"vectors": 100_000, "tenants": 20, "clients": 20, "backlog_messages": 1_000}
EXPECTED_CEILINGS = {
    "duration_seconds": 1_200,
    "storage_bytes": 1_500_000_000,
    "clients": 20,
    "external_cost_usd": 0,
}
EXPECTED_INDEX = "semantic_memory_vectors_tenant_namespace_profile_embedding_idx"
SCHEMA_VERSION = "hindsight.capacity_qualification.v1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _matches_exact_integers(value: Any, expected: dict[str, int]) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(expected)
        and all(type(value[key]) is int and value[key] == expected[key] for key in expected)
    )


def _measurement_map(rows: list[Any]) -> dict[str, dict[str, Any]]:
    if any(not isinstance(row, dict) or not isinstance(row.get("name"), str) for row in rows):
        raise ValueError("capacity raw measurements must be named objects")
    values = {row["name"]: row for row in rows}
    if len(values) != len(rows):
        raise ValueError("capacity raw measurement names must be unique")
    return values


def _validate_measurements(rows: list[Any]) -> None:
    values = _measurement_map(rows)
    if set(values) != {
        "vector_seed",
        "vector_counts",
        "bounded_clients",
        "synthetic_backlog",
        "storage",
        "total",
    }:
        raise ValueError("capacity evidence does not contain the complete measurement set")
    seed = values["vector_seed"]
    storage_checks = seed.get("storage_checks")
    if (
        type(seed.get("batches")) is not int
        or seed["batches"] != TARGETS["tenants"]
        or not isinstance(storage_checks, list)
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
    if (
        type(counts.get("total")) is not int
        or counts["total"] != TARGETS["vectors"]
        or not isinstance(per_tenant, list)
        or len(per_tenant) != TARGETS["tenants"]
        or any(
            not isinstance(row, dict)
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
    if type(storage) is not int or not 0 < storage <= EXPECTED_CEILINGS["storage_bytes"]:
        raise ValueError("capacity measurements exceed or omit the storage ceiling")
    if (
        type(duration) not in {int, float}
        or not 0 < duration <= EXPECTED_CEILINGS["duration_seconds"]
    ):
        raise ValueError("capacity measurements exceed or omit the duration ceiling")


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
    if qualification_link.get("qualified") is not True:
        raise ValueError("capacity evidence requires a qualified populated vector index")
    if re.fullmatch(r"[0-9a-f]{64}", str(qualification_link.get("artifact_sha256") or "")) is None:
        raise ValueError("capacity evidence requires a full SHA-256 qualification artifact digest")
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
    if not document.get("method") or not document.get("environment"):
        raise ValueError("capacity evidence requires method and environment")
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
        or type(qualification.get("vector_dimensions")) is not int
        or qualification["vector_dimensions"] != 1024
        or type(qualification.get("vector_count")) is not int
        or qualification["vector_count"] != TARGETS["vectors"]
        or type(qualification.get("tenant_count")) is not int
        or qualification["tenant_count"] != TARGETS["tenants"]
    ):
        raise ValueError("index qualification does not prove the exact populated target")
    counts = qualification.get("per_tenant_counts")
    if (
        not isinstance(counts, list)
        or len(counts) != TARGETS["tenants"]
        or any(
            not isinstance(row, dict)
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
        or not str(cleanup.get("database", "")).startswith("hindsight_capacity_")
        or cleanup.get("database_removed") is not True
        or cleanup.get("source_revision") != source_revision
        or type(cleanup.get("timeout_seconds")) is not int
        or cleanup["timeout_seconds"] != 120
    ):
        raise ValueError("capacity evidence requires verified disposable-state cleanup")
    expected_names = {"index-qualification.json", "capacity-report.json", "cleanup.json"}
    if set(artifact_digests) != expected_names:
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
