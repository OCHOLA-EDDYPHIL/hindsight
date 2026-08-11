"""Validate the evidence-ineligible resource diagnostic before final qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from validate_capacity_evidence import (  # noqa: E402
    DIAGNOSTIC_SCHEMA_VERSION,
    DIAGNOSTIC_TARGETS,
    EXPECTED_CEILINGS,
    EXPECTED_DATABASE_METHOD,
    EXPECTED_FIXTURE_VECTOR_INDEXES,
    EXPECTED_INDEX,
    EXPECTED_INDEXES,
    EXPECTED_RUNTIME_MEMORY_ENVELOPE,
    EXPECTED_SEEDING_METHOD,
    EXPECTED_VECTOR_BACKFILL_MERGE_BATCH_METHOD,
    EXPECTED_VECTOR_METHOD,
    MAX_DIAGNOSTIC_SAMPLED_PEAK_BYTES,
    MAX_PROJECTED_DURATION_SECONDS,
    TARGETS,
    _matches_exact_integers,
    _validate_execution_id,
    _validate_infrastructure_cleanup,
    _validate_measurements,
    _validate_runtime,
)


DATABASE_PATTERN = re.compile(r"hindsight_capacity_[a-z0-9]{8,20}")
FORBIDDEN_FINAL_ARTIFACTS = frozenset(
    {
        "index-qualification.json",
        "capacity-report.json",
        "artifact-manifest.json",
        "validated-capacity-report.json",
    }
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(
    document: dict[str, Any],
    *,
    source_revision: str,
    execution_id: str,
    cleanup: dict[str, Any],
    runtime: dict[str, Any],
    infrastructure_cleanup: dict[str, Any],
    artifact_digests: dict[str, str],
) -> dict[str, Any]:
    expected_document_keys = {
        "schema_version",
        "kind",
        "mode",
        "acceptance_eligible",
        "qualification_evidence",
        "source_revision",
        "execution_id",
        "targets",
        "final_targets",
        "method",
        "environment",
        "ceilings",
        "raw_measurements",
        "index_observation",
        "cleanup",
        "limitations",
    }
    if (
        re.fullmatch(r"[0-9a-f]{40}", source_revision) is None
        or set(document) != expected_document_keys
        or document.get("schema_version") != DIAGNOSTIC_SCHEMA_VERSION
        or document.get("kind") != "capacity_resource_diagnostic"
        or document.get("mode") != "diagnostic"
        or document.get("acceptance_eligible") is not False
        or document.get("qualification_evidence") is not False
        or document.get("source_revision") != source_revision
        or document.get("execution_id") != execution_id
        or not _matches_exact_integers(document.get("targets"), DIAGNOSTIC_TARGETS)
        or not _matches_exact_integers(document.get("final_targets"), TARGETS)
        or not _matches_exact_integers(document.get("ceilings"), EXPECTED_CEILINGS)
    ):
        raise ValueError("capacity diagnostic identity or bounded targets are invalid")
    _validate_execution_id(execution_id, mode="diagnostic")
    method = document.get("method")
    environment = document.get("environment")
    if (
        not isinstance(method, dict)
        or method.get("database") != EXPECTED_DATABASE_METHOD
        or method.get("vectors") != EXPECTED_VECTOR_METHOD
        or method.get("seeding") != EXPECTED_SEEDING_METHOD
        or method.get("fixture_vector_indexes") != EXPECTED_FIXTURE_VECTOR_INDEXES
        or method.get("vector_backfill_merge_batch")
        != EXPECTED_VECTOR_BACKFILL_MERGE_BATCH_METHOD
        or method.get("clients") != "20_bounded_parallel_index_queries"
        or not isinstance(environment, dict)
        or environment.get("isolation") != "run_scoped_database_and_compose_project"
        or environment.get("paid_model_calls") != 0
        or type(environment.get("paid_model_calls")) is not int
        or environment.get("live_worker_invocations") != 0
        or type(environment.get("live_worker_invocations")) is not int
        or environment.get("runtime_memory_envelope") != EXPECTED_RUNTIME_MEMORY_ENVELOPE
    ):
        raise ValueError("capacity diagnostic method or environment is invalid")
    measurements = document.get("raw_measurements")
    if not isinstance(measurements, list):
        raise ValueError("capacity diagnostic measurements are missing")
    values = _validate_measurements(measurements, targets=DIAGNOSTIC_TARGETS)
    observation = document.get("index_observation")
    per_tenant = observation.get("per_tenant_counts") if isinstance(observation, dict) else None
    plans = observation.get("plans") if isinstance(observation, dict) else None
    tenant_ids = (
        [row.get("tenant_id") for row in per_tenant if isinstance(row, dict)]
        if isinstance(per_tenant, list)
        else []
    )
    if (
        not isinstance(observation, dict)
        or "qualified" in observation
        or "qualification_evidence" in observation
        or observation.get("schema_version") != DIAGNOSTIC_SCHEMA_VERSION
        or observation.get("main_sha") != source_revision
        or observation.get("execution_id") != execution_id
        or observation.get("mode") != "diagnostic"
        or observation.get("observation_only") is not True
        or observation.get("index") != EXPECTED_INDEX
        or observation.get("indexes") != EXPECTED_INDEXES
        or observation.get("vector_dimensions") != 1024
        or type(observation.get("vector_dimensions")) is not int
        or observation.get("vector_count") != DIAGNOSTIC_TARGETS["vectors"]
        or type(observation.get("vector_count")) is not int
        or observation.get("tenant_count") != DIAGNOSTIC_TARGETS["tenants"]
        or type(observation.get("tenant_count")) is not int
        or not isinstance(per_tenant, list)
        or len(per_tenant) != DIAGNOSTIC_TARGETS["tenants"]
        or len(set(tenant_ids)) != DIAGNOSTIC_TARGETS["tenants"]
        or not isinstance(plans, list)
        or len(plans) != DIAGNOSTIC_TARGETS["clients"]
        or [row.get("tenant_id") for row in plans if isinstance(row, dict)]
        != [tenant_ids[number % len(tenant_ids)] for number in range(DIAGNOSTIC_TARGETS["clients"])]
        or any(
            not isinstance(row, dict) or row.get("qualified_index") != EXPECTED_INDEX
            for row in plans
        )
    ):
        raise ValueError("capacity diagnostic index observation is invalid")
    if per_tenant != values["vector_counts"].get("per_tenant") or plans != values[
        "bounded_clients"
    ].get("clients"):
        raise ValueError("capacity diagnostic observations differ from raw measurements")
    cleanup_link = document.get("cleanup")
    if (
        not isinstance(cleanup_link, dict)
        or cleanup_link.get("database_removed") is not True
        or cleanup_link.get("execution_id") != execution_id
        or cleanup_link.get("artifact_sha256") != artifact_digests.get("cleanup.json")
        or cleanup.get("schema_version") != DIAGNOSTIC_SCHEMA_VERSION
        or cleanup.get("source_revision") != source_revision
        or cleanup.get("mode") != "diagnostic"
        or cleanup.get("execution_id") != execution_id
        or cleanup.get("database")
        != f"hindsight_capacity_{hashlib.sha256(execution_id.encode()).hexdigest()[:16]}"
        or cleanup.get("database_removed") is not True
        or cleanup.get("error") is not None
        or cleanup.get("timeout_seconds") != 120
        or type(cleanup.get("timeout_seconds")) is not int
    ):
        raise ValueError("capacity diagnostic database cleanup is invalid")
    runtime = _validate_runtime(
        runtime,
        source_revision=source_revision,
        mode="diagnostic",
        execution_id=execution_id,
    )
    if (
        max(
            runtime["cgroup"]["sampled_peak_bytes"],
            runtime["container_cgroup"]["memory_peak_bytes"],
        )
        > MAX_DIAGNOSTIC_SAMPLED_PEAK_BYTES
    ):
        raise ValueError("capacity diagnostic lacks the required memory headroom")
    infrastructure_cleanup = _validate_infrastructure_cleanup(
        infrastructure_cleanup,
        source_revision=source_revision,
        mode="diagnostic",
        execution_id=execution_id,
        project=runtime["compose_project"],
    )
    expected_artifacts = {
        "capacity-diagnostic.json",
        "cleanup.json",
        "runtime-pressure.json",
        "infrastructure-cleanup.json",
    }
    if set(artifact_digests) != expected_artifacts or any(
        re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in artifact_digests.values()
    ):
        raise ValueError("capacity diagnostic artifact digests are incomplete")
    duration = values["total"]["duration_seconds"]
    if runtime["cgroup"]["sampling_elapsed_ns"] < duration * 1_000_000_000:
        raise ValueError("capacity diagnostic sampling does not cover the measured workload")
    projected_duration = math.ceil(duration * 11 / 8)
    if projected_duration > MAX_PROJECTED_DURATION_SECONDS:
        raise ValueError("capacity diagnostic lacks the required timing headroom")
    limitations = document.get("limitations")
    if not isinstance(
        limitations, list
    ) or "cannot be used as final capacity qualification evidence" not in " ".join(
        map(str, limitations)
    ):
        raise ValueError("capacity diagnostic does not disclaim qualification evidence")
    return {
        **document,
        "kind": "capacity_resource_diagnostic_result",
        "acceptance_eligible": False,
        "qualification_evidence": False,
        "gate_passed": True,
        "projection": {
            "method": "whole_diagnostic_duration_scaled_by_conservative_11_over_8",
            "diagnostic_duration_seconds": duration,
            "projected_final_duration_seconds": projected_duration,
            "maximum_projected_duration_seconds": MAX_PROJECTED_DURATION_SECONDS,
            "final_duration_ceiling_seconds": EXPECTED_CEILINGS["duration_seconds"],
            "minimum_headroom_seconds": (
                EXPECTED_CEILINGS["duration_seconds"] - MAX_PROJECTED_DURATION_SECONDS
            ),
        },
        "runtime_pressure": runtime,
        "infrastructure_cleanup": infrastructure_cleanup,
        "artifacts": artifact_digests,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--cleanup", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--infrastructure-cleanup", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    present_forbidden = sorted(
        name for name in FORBIDDEN_FINAL_ARTIFACTS if (args.input.parent / name).exists()
    )
    if present_forbidden:
        raise ValueError("diagnostic directory contains forbidden final evidence artifacts")
    paths = {
        "capacity-diagnostic.json": args.input,
        "cleanup.json": args.cleanup,
        "runtime-pressure.json": args.runtime,
        "infrastructure-cleanup.json": args.infrastructure_cleanup,
    }
    artifacts = {name: _sha256(path) for name, path in paths.items()}
    report = validate(
        json.loads(args.input.read_text()),
        source_revision=args.source_revision,
        execution_id=args.execution_id,
        cleanup=json.loads(args.cleanup.read_text()),
        runtime=json.loads(args.runtime.read_text()),
        infrastructure_cleanup=json.loads(args.infrastructure_cleanup.read_text()),
        artifact_digests=artifacts,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
