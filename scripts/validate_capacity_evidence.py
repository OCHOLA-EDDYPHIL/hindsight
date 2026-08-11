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
DIAGNOSTIC_TARGETS = {
    "vectors": 75_000,
    "tenants": 15,
    "clients": 20,
    "backlog_messages": 1_000,
}
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
VECTOR_BACKFILL_MERGE_BATCH_SETTING = "bulkio.index_backfill.vector_merge_batch_size"
VECTOR_BACKFILL_DEFAULT_MERGE_BATCH_SIZE = 3
VECTOR_BACKFILL_MERGE_BATCH_SIZE = 64
EXPECTED_VECTOR_INSERT_WORKERS = 1
EXPECTED_VECTOR_METHOD = "deterministic_tenant_anchored_13bit_1024d"
EXPECTED_SEEDING_METHOD = (
    "single_bounded_writer_one_atomic_copy_transaction_per_tenant_"
    "between_exact_legacy_index_drop_and_restore"
)
EXPECTED_FIXTURE_VECTOR_INDEXES = (
    "legacy_only_before_seed_then_none_during_copy_then_legacy_restored_"
    "before_populated_tenant_index_migration"
)
EXPECTED_VECTOR_BACKFILL_MERGE_BATCH_METHOD = (
    f"{VECTOR_BACKFILL_MERGE_BATCH_SETTING}={VECTOR_BACKFILL_MERGE_BATCH_SIZE}_run_scoped"
)
EXPECTED_DATABASE_METHOD = (
    "disposable_local_single_node_cockroachdb_in_memory_2_gib_explicit_memory_budgets"
)
SCHEMA_VERSION = "hindsight.capacity_qualification.v6"
DIAGNOSTIC_SCHEMA_VERSION = "hindsight.capacity_resource_diagnostic.v3"
RUNTIME_SCHEMA_VERSION = "hindsight.capacity_runtime.v3"
INFRASTRUCTURE_CLEANUP_SCHEMA_VERSION = "hindsight.capacity_infrastructure_cleanup.v3"
UPTIME_QUANTUM_NS = 10_000_000
MAX_RECORDED_SAMPLE_GAP_NS = 990_000_000
EXPECTED_RUNTIME_MEMORY_ENVELOPE = {
    "image": (
        "cockroachdb/cockroach@sha256:"
        "53f2dea6f5a666551f404bf6c341bde6595964cf786f24ade7d85249ccedecc7"
    ),
    "image_id": (
        "sha256:53f2dea6f5a666551f404bf6c341bde6595964cf786f24ade7d85249ccedecc7"
    ),
    "image_platform": "linux/amd64",
    "execution_topology": "owner_runner_sibling_dind_capacity_cgroup_v2",
    "start_args": [
        "--store=type=mem,size=2GiB",
        "--cache=128MiB",
        "--max-sql-memory=128MiB",
        "--max-tsdb-memory=64MiB",
        "--max-go-memory=3GiB",
    ],
    "capacity_boundary": {
        "cgroup_version": 2,
        "memory_max_bytes": 4 * 1024**3,
        "memory_swap_max": "max",
        "swap_devices": 0,
        "cpu_quota_us": 150_000,
        "cpu_period_us": 100_000,
    },
    "telemetry_probe": {
        "image": (
            "cockroachdb/cockroach@sha256:"
            "53f2dea6f5a666551f404bf6c341bde6595964cf786f24ade7d85249ccedecc7"
        ),
        "image_id": (
            "sha256:53f2dea6f5a666551f404bf6c341bde6595964cf786f24ade7d85249ccedecc7"
        ),
        "image_platform": "linux/amd64",
        "cgroup_namespace": "host",
        "network": "none",
        "read_only": True,
        "user": "65534:65534",
        "cap_drop": ["ALL"],
        "no_new_privileges": True,
        "privileged": False,
        "workspace_mounts": 0,
        "pids_limit": 16,
        "memory_bytes": 32 * 1024**2,
        "nano_cpus": 500_000_000,
        "nominal_sample_sleep_seconds": 0.25,
        "maximum_sample_gap_seconds": 1.0,
    },
    "memory_bytes": {
        "store": 2 * 1024**3,
        "cache": 128 * 1024**2,
        "sql": 128 * 1024**2,
        "tsdb": 64 * 1024**2,
        "go": 3 * 1024**3,
    },
}
EXPECTED_PROCESS_ARGS = [
    "start-single-node",
    "--insecure",
    *EXPECTED_RUNTIME_MEMORY_ENVELOPE["start_args"],
]
EXPECTED_LIVE_PROCESS_ARGS = [
    "/cockroach/cockroach",
    EXPECTED_PROCESS_ARGS[0],
    "--listening-url-file=server_fifo",
    "--pid-file=server_pid",
    "--advertise-addr=127.0.0.1:26257",
    "--certs-dir=certs",
    "--log=file-defaults: {dir: ./cockroach-data/logs}",
    *EXPECTED_PROCESS_ARGS[1:],
]
EXPECTED_EVENT_KEYS = frozenset({"low", "high", "max", "oom", "oom_kill"})
EXPECTED_BOUNDARY_EVENT_KEYS = frozenset(
    {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"}
)
COMPOSE_PROJECT_PATTERN = re.compile(r"hindsight_capacity_[0-9]+_[0-9]+_(diagnostic|qualification)")
EXECUTION_ID_PATTERN = re.compile(r"capacity_[0-9]+_1_(diagnostic|qualification)")
MAX_DIAGNOSTIC_SAMPLED_PEAK_BYTES = int(3.25 * 1024**3)
MAX_PROJECTED_DURATION_SECONDS = 960
DATABASE_PATTERN = re.compile(r"hindsight_capacity_[a-z0-9]{8,20}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_execution_id(execution_id: str, *, mode: str) -> None:
    if EXECUTION_ID_PATTERN.fullmatch(execution_id) is None or not execution_id.endswith(
        f"_{mode}"
    ):
        raise ValueError("capacity execution identity is invalid")


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


def _validate_measurements(
    rows: list[Any], *, targets: dict[str, int] = TARGETS
) -> dict[str, dict[str, Any]]:
    expected_names = [
        "base_migrations",
        "vector_backfill_merge_batch",
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
    vector_backfill_merge_batch = values["vector_backfill_merge_batch"]
    legacy_index_suspension = values["legacy_index_suspension"]
    legacy_index_restore = values["legacy_index_restore"]
    post_seed_migrations = values["post_seed_migrations"]
    index_build_input = values["tenant_index_build_input"]
    vector_indexes = values["vector_indexes"]
    timed_rows = (
        base_migrations,
        vector_backfill_merge_batch,
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
        or vector_backfill_merge_batch.get("setting")
        != VECTOR_BACKFILL_MERGE_BATCH_SETTING
        or type(vector_backfill_merge_batch.get("previous_value")) is not int
        or vector_backfill_merge_batch["previous_value"]
        != VECTOR_BACKFILL_DEFAULT_MERGE_BATCH_SIZE
        or type(vector_backfill_merge_batch.get("configured_value")) is not int
        or vector_backfill_merge_batch["configured_value"]
        != VECTOR_BACKFILL_MERGE_BATCH_SIZE
        or vector_backfill_merge_batch.get("scope")
        != "run_scoped_disposable_cockroachdb_node"
        or vector_backfill_merge_batch.get("next_populated_index_phase")
        != "legacy_index_restore"
        or legacy_index_suspension.get("removed_index") != LEGACY_INDEX
        or legacy_index_suspension.get("before_indexes") != [LEGACY_INDEX]
        or legacy_index_suspension.get("after_indexes") != []
        or legacy_index_restore.get("migration") != LEGACY_VECTOR_MIGRATION
        or legacy_index_restore.get("restored_index") != LEGACY_INDEX
        or legacy_index_restore.get("before_indexes") != []
        or legacy_index_restore.get("after_indexes") != [LEGACY_INDEX]
        or type(legacy_index_restore.get("vectors")) is not int
        or legacy_index_restore["vectors"] != targets["vectors"]
        or type(legacy_index_restore.get("storage_bytes")) is not int
        or not 0
        < legacy_index_restore["storage_bytes"]
        <= EXPECTED_CEILINGS["storage_bytes"]
        or type(index_build_input.get("vectors")) is not int
        or index_build_input["vectors"] != targets["vectors"]
        or index_build_input.get("present_indexes") != [LEGACY_INDEX]
        or index_build_input.get("absent_index") != EXPECTED_INDEX
        or index_build_input.get("next_migration") != TENANT_VECTOR_MIGRATION
        or vector_indexes.get("indexes") != EXPECTED_INDEXES
        or type(vector_indexes.get("storage_bytes")) is not int
        or not 0 < vector_indexes["storage_bytes"] <= EXPECTED_CEILINGS["storage_bytes"]
    ):
        raise ValueError(
            "capacity evidence does not prove the exact run-scoped backfill setting, "
            "index-free seed lifecycle, or populated tenant-index build with both indexes live"
        )
    seed = values["vector_seed"]
    storage_checks = seed.get("storage_checks")
    if (
        type(seed.get("batches")) is not int
        or seed["batches"] != targets["tenants"]
        or type(seed.get("vector_insert_rows")) is not int
        or seed["vector_insert_rows"] != targets["vectors"]
        or type(seed.get("vector_insert_transactions")) is not int
        or seed["vector_insert_transactions"] != targets["tenants"]
        or type(seed.get("vector_insert_workers")) is not int
        or seed["vector_insert_workers"] != EXPECTED_VECTOR_INSERT_WORKERS
        or type(seed.get("vector_insert_client_retries")) is not int
        or seed["vector_insert_client_retries"] != 0
    ):
        raise ValueError("capacity seeding does not prove exact bounded vector insertion")
    if (
        not isinstance(storage_checks, list)
        or len(storage_checks) != targets["tenants"]
        or [row.get("completion_sequence") for row in storage_checks if isinstance(row, dict)]
        != list(range(1, targets["tenants"] + 1))
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
        or counts["total"] != targets["vectors"]
        or not isinstance(per_tenant, list)
        or len(per_tenant) != targets["tenants"]
        or len(set(tenant_ids)) != targets["tenants"]
        or any(
            not isinstance(row, dict)
            or not _is_canonical_uuid(row.get("tenant_id"))
            or type(row.get("vectors")) is not int
            or row["vectors"] != targets["vectors"] // targets["tenants"]
            for row in per_tenant
        )
    ):
        raise ValueError("capacity measurements do not prove exact vector and tenant counts")
    clients = values["bounded_clients"].get("clients")
    if (
        not isinstance(clients, list)
        or len(clients) != targets["clients"]
        or {row.get("client") for row in clients if isinstance(row, dict)}
        != set(range(1, targets["clients"] + 1))
        or [row.get("tenant_id") for row in clients if isinstance(row, dict)]
        != [tenant_ids[number % len(tenant_ids)] for number in range(targets["clients"])]
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
                "messages_enqueued": targets["backlog_messages"],
                "messages_drained": targets["backlog_messages"],
                "messages_accounted_for": targets["backlog_messages"],
                "queue_capacity": targets["backlog_messages"],
                "pending_before_drain": targets["backlog_messages"],
                "pending_after_drain": 0,
                "observed_max_pending": targets["backlog_messages"],
                "clients": targets["clients"],
                "live_worker_invocations": 0,
                "paid_model_calls": 0,
            },
        )
        or not isinstance(per_client, list)
        or len(per_client) != targets["clients"]
        or any(type(count) is not int or count <= 0 for count in per_client)
        or sum(per_client) != targets["backlog_messages"]
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
    return values


def _validate_runtime(
    runtime: dict[str, Any], *, source_revision: str, mode: str, execution_id: str
) -> dict[str, Any]:
    project = runtime.get("compose_project")
    configured = runtime.get("configured")
    process = runtime.get("effective_process")
    container_cgroup = runtime.get("container_cgroup")
    cgroup = runtime.get("cgroup")
    boundary = EXPECTED_RUNTIME_MEMORY_ENVELOPE["capacity_boundary"]
    if (
        runtime.get("schema_version") != RUNTIME_SCHEMA_VERSION
        or runtime.get("source_revision") != source_revision
        or runtime.get("mode") != mode
        or runtime.get("execution_id") != execution_id
        or not isinstance(project, str)
        or COMPOSE_PROJECT_PATTERN.fullmatch(project) is None
        or project != f"hindsight_{execution_id}"
        or configured != EXPECTED_RUNTIME_MEMORY_ENVELOPE
        or not isinstance(process, dict)
        or not isinstance(container_cgroup, dict)
        or not isinstance(cgroup, dict)
    ):
        raise ValueError("capacity runtime envelope identity is invalid")
    if (
        set(process)
        != {
            "path",
            "args",
            "configured_command",
            "image",
            "cgroup_namespace",
            "compose_project",
            "compose_service",
            "running",
            "live_argv",
            "effective_memory",
        }
        or process.get("path") != "/cockroach/cockroach.sh"
        or process.get("args") != EXPECTED_PROCESS_ARGS
        or process.get("configured_command") != EXPECTED_PROCESS_ARGS
        or process.get("image") != EXPECTED_RUNTIME_MEMORY_ENVELOPE["image"]
        or process.get("cgroup_namespace") != "private"
        or process.get("compose_project") != project
        or process.get("compose_service") != "crdb"
        or process.get("running") is not True
        or process.get("live_argv") != EXPECTED_LIVE_PROCESS_ARGS
        or process.get("effective_memory")
        != {
            "go_limit_bytes": EXPECTED_RUNTIME_MEMORY_ENVELOPE["memory_bytes"]["go"],
            "store_capacity_bytes": EXPECTED_RUNTIME_MEMORY_ENVELOPE["memory_bytes"]["store"],
            "store_count": 1,
        }
    ):
        raise ValueError("capacity runtime process does not match the reviewed memory arguments")
    container_events = container_cgroup.get("events")
    if (
        set(container_cgroup)
        != {
            "version",
            "memory_max",
            "memory_current_bytes",
            "memory_peak_bytes",
            "events",
        }
        or container_cgroup.get("version") != 2
        or container_cgroup.get("memory_max")
        not in {"max", boundary["memory_max_bytes"]}
        or type(container_cgroup.get("memory_current_bytes")) is not int
        or container_cgroup["memory_current_bytes"] < 0
        or type(container_cgroup.get("memory_peak_bytes")) is not int
        or not 0
        <= container_cgroup["memory_current_bytes"]
        <= container_cgroup["memory_peak_bytes"]
        < boundary["memory_max_bytes"]
        or container_cgroup["memory_peak_bytes"] == 0
        or not isinstance(container_events, dict)
        or not EXPECTED_EVENT_KEYS.issubset(container_events)
        or any(type(value) is not int or value != 0 for value in container_events.values())
    ):
        raise ValueError("capacity runtime container cgroup recorded memory pressure")
    expected_cgroup_keys = {
        "version",
        "scope",
        "source",
        "memory_max_bytes",
        "memory_swap_max",
        "memory_swap_current_before_bytes",
        "memory_swap_current_after_bytes",
        "swap_devices_before",
        "swap_devices_after",
        "cpu_quota_us",
        "cpu_period_us",
        "memory_current_before_bytes",
        "memory_current_after_bytes",
        "kernel_memory_peak_before_bytes",
        "kernel_memory_peak_after_bytes",
        "nominal_sample_sleep_seconds",
        "maximum_sample_gap_seconds",
        "observed_max_sample_gap_ns",
        "sampling_elapsed_ns",
        "baseline_sequence",
        "baseline_monotonic_ns",
        "workload_stop_observed_monotonic_ns",
        "workload_last_sequence",
        "workload_last_monotonic_ns",
        "post_teardown_observed_monotonic_ns",
        "post_teardown_sample_monotonic_ns",
        "final_snapshot_monotonic_ns",
        "sample_count",
        "sampled_peak_bytes",
        "events_before",
        "events_after",
        "event_deltas",
        "pressure_events_zero",
    }
    before = cgroup.get("events_before")
    after = cgroup.get("events_after")
    deltas = cgroup.get("event_deltas")
    integer_fields = (
        "memory_current_before_bytes",
        "memory_current_after_bytes",
        "kernel_memory_peak_before_bytes",
        "kernel_memory_peak_after_bytes",
        "memory_swap_current_before_bytes",
        "memory_swap_current_after_bytes",
        "swap_devices_before",
        "swap_devices_after",
        "sample_count",
        "sampled_peak_bytes",
        "observed_max_sample_gap_ns",
        "sampling_elapsed_ns",
        "baseline_sequence",
        "baseline_monotonic_ns",
        "workload_stop_observed_monotonic_ns",
        "workload_last_sequence",
        "workload_last_monotonic_ns",
        "post_teardown_observed_monotonic_ns",
        "post_teardown_sample_monotonic_ns",
        "final_snapshot_monotonic_ns",
    )
    uptime_fields = (
        "baseline_monotonic_ns",
        "workload_stop_observed_monotonic_ns",
        "workload_last_monotonic_ns",
        "post_teardown_observed_monotonic_ns",
        "post_teardown_sample_monotonic_ns",
        "final_snapshot_monotonic_ns",
    )
    gap_fields = ("observed_max_sample_gap_ns", "sampling_elapsed_ns")
    if (
        set(cgroup) != expected_cgroup_keys
        or cgroup.get("version") != 2
        or cgroup.get("scope") != "sibling_dind_daemon_and_descendants"
        or cgroup.get("source") != "sandboxed_cgroupns_host_probe"
        or cgroup.get("memory_max_bytes") != boundary["memory_max_bytes"]
        or cgroup.get("memory_swap_max") != boundary["memory_swap_max"]
        or cgroup.get("cpu_quota_us") != boundary["cpu_quota_us"]
        or cgroup.get("cpu_period_us") != boundary["cpu_period_us"]
        or cgroup.get("nominal_sample_sleep_seconds") != 0.25
        or cgroup.get("maximum_sample_gap_seconds") != 1.0
        or any(type(cgroup.get(key)) is not int or cgroup[key] < 0 for key in integer_fields)
        or any(
            cgroup[key] <= 0 or cgroup[key] % UPTIME_QUANTUM_NS != 0
            for key in uptime_fields
        )
        or any(
            cgroup[key] <= 0 or cgroup[key] % UPTIME_QUANTUM_NS != 0
            for key in gap_fields
        )
        or cgroup["memory_swap_current_before_bytes"] != 0
        or cgroup["memory_swap_current_after_bytes"] != 0
        or cgroup["swap_devices_before"] != boundary["swap_devices"]
        or cgroup["swap_devices_after"] != boundary["swap_devices"]
        or cgroup["memory_current_before_bytes"] > boundary["memory_max_bytes"]
        or cgroup["memory_current_after_bytes"] > boundary["memory_max_bytes"]
        or not 3 <= cgroup["sample_count"] <= 7_201
        or cgroup["baseline_sequence"] != 0
        or not 0 < cgroup["observed_max_sample_gap_ns"] <= MAX_RECORDED_SAMPLE_GAP_NS
        or cgroup["observed_max_sample_gap_ns"] > cgroup["sampling_elapsed_ns"]
        or cgroup["sampling_elapsed_ns"]
        > cgroup["observed_max_sample_gap_ns"] * (cgroup["sample_count"] - 1)
        or cgroup["sampling_elapsed_ns"]
        != cgroup["final_snapshot_monotonic_ns"] - cgroup["baseline_monotonic_ns"]
        or not cgroup["baseline_monotonic_ns"]
        <= cgroup["workload_stop_observed_monotonic_ns"]
        <= cgroup["workload_last_monotonic_ns"]
        < cgroup["post_teardown_observed_monotonic_ns"]
        <= cgroup["post_teardown_sample_monotonic_ns"]
        < cgroup["final_snapshot_monotonic_ns"]
        or cgroup["workload_last_monotonic_ns"]
        - cgroup["workload_stop_observed_monotonic_ns"]
        > MAX_RECORDED_SAMPLE_GAP_NS
        or cgroup["post_teardown_sample_monotonic_ns"]
        - cgroup["post_teardown_observed_monotonic_ns"]
        > MAX_RECORDED_SAMPLE_GAP_NS
        or not 0 <= cgroup["workload_last_sequence"] < cgroup["sample_count"] - 2
        or not 0 < cgroup["sampled_peak_bytes"] < cgroup["memory_max_bytes"]
        or cgroup["sampled_peak_bytes"] < cgroup["memory_current_before_bytes"]
        or cgroup["sampled_peak_bytes"] < cgroup["memory_current_after_bytes"]
        or cgroup["kernel_memory_peak_before_bytes"]
        < cgroup["memory_current_before_bytes"]
        or cgroup["kernel_memory_peak_after_bytes"]
        < cgroup["kernel_memory_peak_before_bytes"]
        or cgroup["kernel_memory_peak_after_bytes"]
        < cgroup["memory_current_after_bytes"]
        or cgroup["kernel_memory_peak_after_bytes"] < cgroup["sampled_peak_bytes"]
        or not isinstance(before, dict)
        or not isinstance(after, dict)
        or not isinstance(deltas, dict)
        or set(before) != set(after)
        or set(before) != set(deltas)
        or set(before) != EXPECTED_BOUNDARY_EVENT_KEYS
    ):
        raise ValueError("capacity runtime cgroup telemetry is incomplete")
    if (
        any(
            type(before[key]) is not int
            or type(after[key]) is not int
            or type(deltas[key]) is not int
            or before[key] < 0
            or after[key] < before[key]
            or deltas[key] != after[key] - before[key]
            or deltas[key] != 0
            for key in before
        )
        or cgroup.get("pressure_events_zero") is not True
    ):
        raise ValueError("capacity runtime recorded memory-pressure events")
    return runtime


def _validate_infrastructure_cleanup(
    cleanup: dict[str, Any],
    *,
    source_revision: str,
    mode: str,
    execution_id: str,
    project: str,
) -> dict[str, Any]:
    if (
        set(cleanup)
        != {
            "schema_version",
            "source_revision",
            "mode",
            "execution_id",
            "compose_project",
            "down_status",
            "runtime_evidence_capture_status",
            "runtime_finalize_status",
            "probe_cleanup_status",
            "container_query_status",
            "volume_query_status",
            "network_query_status",
            "probe_query_status",
            "remaining_containers",
            "remaining_volumes",
            "remaining_networks",
            "remaining_probes",
            "compose_state_removed",
        }
        or cleanup.get("schema_version") != INFRASTRUCTURE_CLEANUP_SCHEMA_VERSION
        or cleanup.get("source_revision") != source_revision
        or cleanup.get("mode") != mode
        or cleanup.get("execution_id") != execution_id
        or cleanup.get("compose_project") != project
        or type(cleanup.get("down_status")) is not int
        or cleanup["down_status"] != 0
        or type(cleanup.get("runtime_evidence_capture_status")) is not int
        or cleanup["runtime_evidence_capture_status"] != 0
        or type(cleanup.get("runtime_finalize_status")) is not int
        or cleanup["runtime_finalize_status"] != 0
        or type(cleanup.get("probe_cleanup_status")) is not int
        or cleanup["probe_cleanup_status"] != 0
        or type(cleanup.get("container_query_status")) is not int
        or cleanup["container_query_status"] != 0
        or type(cleanup.get("volume_query_status")) is not int
        or cleanup["volume_query_status"] != 0
        or type(cleanup.get("network_query_status")) is not int
        or cleanup["network_query_status"] != 0
        or type(cleanup.get("probe_query_status")) is not int
        or cleanup["probe_query_status"] != 0
        or type(cleanup.get("remaining_containers")) is not int
        or cleanup["remaining_containers"] != 0
        or type(cleanup.get("remaining_volumes")) is not int
        or cleanup["remaining_volumes"] != 0
        or type(cleanup.get("remaining_networks")) is not int
        or cleanup["remaining_networks"] != 0
        or type(cleanup.get("remaining_probes")) is not int
        or cleanup["remaining_probes"] != 0
        or cleanup.get("compose_state_removed") is not True
    ):
        raise ValueError("capacity evidence requires verified Compose cleanup")
    return cleanup


def validate(
    document: dict[str, Any],
    *,
    source_revision: str,
    execution_id: str,
    qualification: dict[str, Any] | None = None,
    cleanup: dict[str, Any] | None = None,
    runtime: dict[str, Any] | None = None,
    infrastructure_cleanup: dict[str, Any] | None = None,
    artifact_digests: dict[str, str] | None = None,
) -> dict[str, Any]:
    if (
        qualification is None
        or cleanup is None
        or runtime is None
        or infrastructure_cleanup is None
        or artifact_digests is None
    ):
        raise ValueError(
            "capacity validation requires qualification, cleanup, and artifact manifest evidence "
            "plus runtime and infrastructure cleanup"
        )
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    _validate_execution_id(execution_id, mode="qualification")
    qualification_link = document.get("index_qualification") or {}
    cleanup_link = document.get("cleanup") or {}
    if qualification_link.get("qualified") is not True:
        raise ValueError("capacity evidence requires a qualified populated vector index")
    if re.fullmatch(r"[0-9a-f]{64}", str(qualification_link.get("artifact_sha256") or "")) is None:
        raise ValueError("capacity evidence requires a full SHA-256 qualification artifact digest")
    if (
        cleanup_link.get("database_removed") is not True
        or re.fullmatch(r"[0-9a-f]{64}", str(cleanup_link.get("artifact_sha256") or "")) is None
    ):
        raise ValueError("capacity evidence requires a bound cleanup artifact")
    if qualification_link.get("main_sha") != source_revision:
        raise ValueError("index qualification must belong to the exact tested main revision")
    if (
        document.get("source_revision") != source_revision
        or document.get("execution_id") != execution_id
        or qualification_link.get("execution_id") != execution_id
        or cleanup_link.get("execution_id") != execution_id
    ):
        raise ValueError("capacity evidence must belong to the exact tested main revision")
    if (
        document.get("schema_version") != SCHEMA_VERSION
        or document.get("kind") != "bounded_capacity_evidence_source"
        or document.get("mode") != "qualification"
        or document.get("qualification_evidence") is not True
    ):
        raise ValueError("capacity evidence schema version is unsupported")
    if not _matches_exact_integers(document.get("targets"), TARGETS):
        raise ValueError("capacity evidence does not match the bounded target shape")
    if not _matches_exact_integers(document.get("final_targets"), TARGETS):
        raise ValueError("capacity evidence does not preserve the final bounded target")
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
    if (
        method.get("vector_backfill_merge_batch")
        != EXPECTED_VECTOR_BACKFILL_MERGE_BATCH_METHOD
    ):
        raise ValueError("capacity evidence does not identify the bounded vector backfill method")
    if method.get("clients") != "20_bounded_parallel_index_queries":
        raise ValueError("capacity evidence does not identify twenty bounded clients")
    environment = document["environment"]
    if (
        not isinstance(environment, dict)
        or type(environment.get("paid_model_calls")) is not int
        or environment["paid_model_calls"] != 0
        or type(environment.get("live_worker_invocations")) is not int
        or environment["live_worker_invocations"] != 0
        or environment.get("isolation") != "run_scoped_database_and_compose_project"
        or environment.get("runtime_memory_envelope") != EXPECTED_RUNTIME_MEMORY_ENVELOPE
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
        or qualification.get("observation_only") is not False
        or qualification.get("mode") != "qualification"
        or qualification.get("qualification_evidence") is not True
        or qualification.get("main_sha") != source_revision
        or qualification.get("execution_id") != execution_id
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
        or cleanup.get("mode") != "qualification"
        or cleanup.get("execution_id") != execution_id
        or cleanup.get("database")
        != f"hindsight_capacity_{hashlib.sha256(execution_id.encode()).hexdigest()[:16]}"
        or cleanup.get("database_removed") is not True
        or "error" not in cleanup
        or cleanup.get("error") is not None
        or cleanup.get("source_revision") != source_revision
        or type(cleanup.get("timeout_seconds")) is not int
        or cleanup["timeout_seconds"] != 120
    ):
        raise ValueError("capacity evidence requires verified disposable-state cleanup")
    runtime = _validate_runtime(
        runtime,
        source_revision=source_revision,
        mode="qualification",
        execution_id=execution_id,
    )
    if (
        runtime["cgroup"]["sampling_elapsed_ns"]
        < measured["total"]["duration_seconds"] * 1_000_000_000
    ):
        raise ValueError("capacity runtime sampling does not cover the measured workload")
    infrastructure_cleanup = _validate_infrastructure_cleanup(
        infrastructure_cleanup,
        source_revision=source_revision,
        mode="qualification",
        execution_id=execution_id,
        project=runtime["compose_project"],
    )
    expected_names = {
        "index-qualification.json",
        "capacity-report.json",
        "cleanup.json",
        "runtime-pressure.json",
        "infrastructure-cleanup.json",
    }
    if (
        set(artifact_digests) != expected_names
        or any(
            re.fullmatch(r"[0-9a-f]{64}", str(digest)) is None
            for digest in artifact_digests.values()
        )
        or artifact_digests["index-qualification.json"] != qualification_link["artifact_sha256"]
        or artifact_digests["cleanup.json"] != cleanup_link["artifact_sha256"]
    ):
        raise ValueError("capacity artifact manifest is incomplete")
    return {
        **document,
        "kind": "bounded_capacity_evidence",
        "claim_scope": "benchmark_evidence_not_production_slo",
        "runtime_pressure": runtime,
        "infrastructure_cleanup": infrastructure_cleanup,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--execution-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, required=True)
    parser.add_argument("--cleanup", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--infrastructure-cleanup", type=Path, required=True)
    args = parser.parse_args()
    document = json.loads(args.input.read_text())
    qualification = json.loads(args.qualification.read_text())
    cleanup = json.loads(args.cleanup.read_text())
    manifest = json.loads(args.manifest.read_text())
    runtime = json.loads(args.runtime.read_text())
    infrastructure_cleanup = json.loads(args.infrastructure_cleanup.read_text())
    digests = manifest.get("artifacts") if isinstance(manifest, dict) else None
    actual = {
        "index-qualification.json": _sha256(args.qualification),
        "capacity-report.json": _sha256(args.input),
        "cleanup.json": _sha256(args.cleanup),
        "runtime-pressure.json": _sha256(args.runtime),
        "infrastructure-cleanup.json": _sha256(args.infrastructure_cleanup),
    }
    if (
        digests != actual
        or manifest.get("source_revision") != args.source_revision
        or manifest.get("execution_id") != args.execution_id
        or manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("mode") != "qualification"
        or manifest.get("kind") != "capacity_artifact_manifest"
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
        execution_id=args.execution_id,
        qualification=qualification,
        cleanup=cleanup,
        runtime=runtime,
        infrastructure_cleanup=infrastructure_cleanup,
        artifact_digests=digests,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
