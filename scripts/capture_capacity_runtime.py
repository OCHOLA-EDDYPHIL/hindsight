"""Capture and bind the exact runtime envelope for capacity qualification."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASELINE_SCHEMA_VERSION = "hindsight.capacity_runtime_baseline.v1"
SAMPLES_SCHEMA_VERSION = "hindsight.capacity_runtime_samples.v1"
RUNTIME_SCHEMA_VERSION = "hindsight.capacity_runtime.v1"
CAPACITY_SCHEMA_VERSION = "hindsight.capacity_qualification.v4"
MODES = frozenset({"diagnostic", "qualification"})
EXPECTED_IMAGE = "cockroachdb/cockroach:v25.4.5"
EXPECTED_RUNNER_TOPOLOGY = "owner_runner_dind_inside_sampled_job_cgroup"
EXPECTED_START_ARGS = (
    "--store=type=mem,size=2GiB",
    "--cache=128MiB",
    "--max-sql-memory=128MiB",
    "--max-tsdb-memory=64MiB",
    "--max-go-memory=3GiB",
)
EXPECTED_START_ARGS_TEXT = " ".join(EXPECTED_START_ARGS)
EXPECTED_PROCESS_ARGS = ("start-single-node", "--insecure", *EXPECTED_START_ARGS)
EXPECTED_LIVE_PROCESS_ARGS = (
    "/cockroach/cockroach",
    EXPECTED_PROCESS_ARGS[0],
    "--listening-url-file=server_fifo",
    "--pid-file=server_pid",
    "--advertise-addr=127.0.0.1:26257",
    "--certs-dir=certs",
    "--log=file-defaults: {dir: ./cockroach-data/logs}",
    *EXPECTED_PROCESS_ARGS[1:],
)
EXPECTED_RUNNER_MEMORY_BYTES = 4 * 1024**3
EXPECTED_CPU_QUOTA_US = 150_000
EXPECTED_CPU_PERIOD_US = 100_000
EXPECTED_MEMORY_BYTES = {
    "store": 2 * 1024**3,
    "cache": 128 * 1024**2,
    "sql": 128 * 1024**2,
    "tsdb": 64 * 1024**2,
    "go": 3 * 1024**3,
}
REQUIRED_EVENT_KEYS = frozenset({"low", "high", "max", "oom", "oom_kill"})
SOURCE_PATTERN = re.compile(r"[0-9a-f]{40}")
EXECUTION_ID_PATTERN = re.compile(r"capacity_[0-9]+_1_(diagnostic|qualification)")
COMPOSE_PROJECT_PATTERN = re.compile(r"hindsight_capacity_[0-9]+_[0-9]+_(diagnostic|qualification)")
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
SAMPLE_INTERVAL_SECONDS = 0.25
MAX_MONITOR_SECONDS = 1_600
DOCKER_COMMAND_TIMEOUT_SECONDS = 15


def _current_cgroup_leaf() -> Path:
    root = Path("/sys/fs/cgroup")
    if not (root / "cgroup.controllers").is_file():
        raise RuntimeError("capacity telemetry requires cgroup v2")
    relative: str | None = None
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            relative = line[3:]
            break
    if relative is None or ".." in Path(relative).parts:
        raise RuntimeError("capacity telemetry cannot resolve the current cgroup")
    candidate = root / relative.lstrip("/")
    if root != candidate and root not in candidate.parents:
        raise RuntimeError("capacity telemetry resolved a cgroup outside the v2 mount")
    return candidate if candidate.is_dir() else root


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return document


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_nonnegative_integer(raw: str, *, name: str) -> int:
    if re.fullmatch(r"0|[1-9][0-9]*", raw) is None:
        raise ValueError(f"{name} is not a canonical non-negative integer")
    return int(raw)


def _read_integer(path: Path) -> int:
    return _parse_nonnegative_integer(path.read_text().strip(), name=path.name)


def _read_events(path: Path) -> dict[str, int]:
    events: dict[str, int] = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] in events:
            raise ValueError("cgroup memory.events is malformed")
        events[parts[0]] = _parse_nonnegative_integer(parts[1], name=parts[0])
    if not REQUIRED_EVENT_KEYS.issubset(events):
        raise ValueError("cgroup memory.events omits required pressure counters")
    return events


def _current_cgroup_directory() -> Path:
    root = Path("/sys/fs/cgroup")
    candidate = _current_cgroup_leaf()
    if not (candidate / "memory.current").is_file():
        candidate = root
    candidates: list[tuple[Path, int]] = []
    current = candidate
    while True:
        maximum_path = current / "memory.max"
        if maximum_path.is_file():
            raw = maximum_path.read_text().strip()
            if raw != "max":
                candidates.append((current, _parse_nonnegative_integer(raw, name="memory.max")))
        if current == root:
            break
        if root not in current.parents:
            raise RuntimeError("capacity telemetry resolved a cgroup outside the v2 mount")
        current = current.parent
    if not candidates:
        raise RuntimeError("capacity telemetry requires a finite cgroup memory limit")
    directory, _maximum = min(candidates, key=lambda row: row[1])
    required = ("memory.current", "memory.peak", "memory.events", "memory.max")
    if any(not (directory / name).is_file() for name in required):
        raise RuntimeError("capacity telemetry cgroup is missing memory controls")
    return directory


def _snapshot(directory: Path, *, cpu_directory: Path | None = None) -> dict[str, Any]:
    stat = directory.stat()
    maximum = (directory / "memory.max").read_text().strip()
    if maximum == "max":
        raise RuntimeError("capacity telemetry selected an unbounded cgroup")
    cpu_candidates: list[tuple[int, int]] = []
    root = Path("/sys/fs/cgroup")
    current = cpu_directory or _current_cgroup_leaf()
    while True:
        cpu_max = current / "cpu.max"
        if cpu_max.is_file():
            parts = cpu_max.read_text().split()
            if len(parts) != 2:
                raise ValueError("cgroup cpu.max is malformed")
            if parts[0] != "max":
                cpu_candidates.append(
                    (
                        _parse_nonnegative_integer(parts[0], name="cpu.max quota"),
                        _parse_nonnegative_integer(parts[1], name="cpu.max period"),
                    )
                )
        if current == root:
            break
        current = current.parent
    if not cpu_candidates:
        raise RuntimeError("capacity telemetry requires a finite cgroup CPU quota")
    if any(quota <= 0 or period <= 0 for quota, period in cpu_candidates):
        raise RuntimeError("capacity telemetry cgroup CPU quota is invalid")
    quota, period = min(cpu_candidates, key=lambda row: row[0] / row[1])
    return {
        "identity": {"device": stat.st_dev, "inode": stat.st_ino},
        "memory_max_bytes": _parse_nonnegative_integer(maximum, name="memory.max"),
        "cpu_quota_us": quota,
        "cpu_period_us": period,
        "memory_current_bytes": _read_integer(directory / "memory.current"),
        "kernel_memory_peak_bytes": _read_integer(directory / "memory.peak"),
        "events": _read_events(directory / "memory.events"),
    }


def _configured_envelope() -> dict[str, Any]:
    return {
        "image": EXPECTED_IMAGE,
        "runner_topology": EXPECTED_RUNNER_TOPOLOGY,
        "start_args": list(EXPECTED_START_ARGS),
        "runner_memory_max_bytes": EXPECTED_RUNNER_MEMORY_BYTES,
        "runner_cpu": {
            "quota_us": EXPECTED_CPU_QUOTA_US,
            "period_us": EXPECTED_CPU_PERIOD_US,
        },
        "memory_bytes": dict(EXPECTED_MEMORY_BYTES),
    }


def _validate_identity(
    document: dict[str, Any],
    *,
    schema: str,
    source_revision: str,
    mode: str,
    execution_id: str,
    project: str,
) -> None:
    if (
        document.get("schema_version") != schema
        or document.get("source_revision") != source_revision
        or document.get("mode") != mode
        or document.get("execution_id") != execution_id
        or document.get("compose_project") != project
        or document.get("configured") != _configured_envelope()
    ):
        raise ValueError("capacity runtime telemetry identity is invalid")


def _validate_invocation(source_revision: str, mode: str, execution_id: str, project: str) -> None:
    if SOURCE_PATTERN.fullmatch(source_revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    if mode not in MODES:
        raise ValueError("capacity runtime mode is invalid")
    if (
        EXECUTION_ID_PATTERN.fullmatch(execution_id) is None
        or not execution_id.endswith(f"_{mode}")
        or COMPOSE_PROJECT_PATTERN.fullmatch(project) is None
        or project != f"hindsight_{execution_id}"
    ):
        raise ValueError("capacity runtime Compose project is invalid")
    if os.environ.get("EXECUTION_ID") != execution_id:
        raise ValueError("capacity runtime execution identity differs from the environment")
    if os.environ.get("COMPOSE_PROJECT_NAME") != project:
        raise ValueError("capacity runtime Compose project differs from the environment")
    if os.environ.get("COCKROACH_IMAGE") != EXPECTED_IMAGE:
        raise ValueError("capacity runtime CockroachDB image differs from the reviewed image")
    if os.environ.get("COCKROACH_START_ARGS") != EXPECTED_START_ARGS_TEXT:
        raise ValueError("capacity runtime memory arguments differ from the reviewed envelope")


def _inspect_container(project: str) -> dict[str, Any] | None:
    listed = subprocess.run(
        ["docker", "compose", "ps", "-q", "crdb"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    if listed.returncode != 0:
        return None
    identifiers = [row.strip() for row in listed.stdout.splitlines() if row.strip()]
    if not identifiers:
        return None
    if len(identifiers) != 1 or CONTAINER_ID_PATTERN.fullmatch(identifiers[0]) is None:
        raise RuntimeError("capacity runtime resolved an invalid CockroachDB container")
    inspected = subprocess.run(
        ["docker", "inspect", identifiers[0]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    payload = json.loads(inspected.stdout)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("capacity runtime Docker inspection is malformed")
    row = payload[0]
    config = row.get("Config")
    state = row.get("State")
    if not isinstance(config, dict) or not isinstance(state, dict):
        raise RuntimeError("capacity runtime Docker inspection is incomplete")
    labels = config.get("Labels")
    process = {
        "path": row.get("Path"),
        "args": row.get("Args"),
        "configured_command": config.get("Cmd"),
        "image": config.get("Image"),
        "compose_project": labels.get("com.docker.compose.project")
        if isinstance(labels, dict)
        else None,
        "compose_service": labels.get("com.docker.compose.service")
        if isinstance(labels, dict)
        else None,
        "running": state.get("Running"),
    }
    if (
        process["path"] != "/cockroach/cockroach.sh"
        or process["args"] != list(EXPECTED_PROCESS_ARGS)
        or process["configured_command"] != list(EXPECTED_PROCESS_ARGS)
        or process["image"] != EXPECTED_IMAGE
        or process["compose_project"] != project
        or process["compose_service"] != "crdb"
        or process["running"] is not True
    ):
        raise RuntimeError("capacity CockroachDB process differs from the reviewed envelope")
    pid_raw = _try_read_container_file("/cockroach/server_pid")
    if pid_raw is None:
        return None
    pid = _parse_nonnegative_integer(pid_raw, name="CockroachDB server PID")
    if pid == 0:
        raise RuntimeError("capacity CockroachDB server PID is invalid")
    live_raw = _try_read_container_file(f"/proc/{pid}/cmdline")
    if not live_raw:
        return None
    live_argv = [part for part in live_raw.split("\0") if part]
    if live_argv != list(EXPECTED_LIVE_PROCESS_ARGS):
        raise RuntimeError("capacity CockroachDB live argv differs from the reviewed envelope")
    process["live_argv"] = live_argv
    return process


def _query_single_integer(statement: str) -> int | None:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "crdb",
            "cockroach",
            "sql",
            "--insecure",
            "--format=csv",
            "--execute",
            statement,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        return None
    rows = list(csv.reader(completed.stdout.splitlines()))
    if len(rows) != 2 or len(rows[0]) != 1 or len(rows[1]) != 1:
        raise RuntimeError("capacity runtime metric query returned an unexpected shape")
    return _parse_nonnegative_integer(rows[1][0], name=rows[0][0])


def _inspect_effective_memory() -> dict[str, int] | None:
    go_limit = _query_single_integer(
        "SELECT value::INT8 AS go_limit_bytes FROM crdb_internal.node_metrics "
        "WHERE name = 'sys.go.limitbytes'"
    )
    if go_limit is None:
        return None
    if go_limit == 0:
        return None
    store_capacity = _query_single_integer(
        "SELECT capacity::INT8 AS store_capacity_bytes FROM crdb_internal.kv_store_status"
    )
    if store_capacity is None:
        return None
    if store_capacity == 0:
        return None
    if go_limit != EXPECTED_MEMORY_BYTES["go"] or store_capacity != EXPECTED_MEMORY_BYTES["store"]:
        raise RuntimeError("capacity runtime effective database memory differs from configuration")
    return {
        "go_limit_bytes": go_limit,
        "store_capacity_bytes": store_capacity,
        "store_count": 1,
    }


def _read_container_file(path: str) -> str:
    completed = subprocess.run(
        ["docker", "compose", "exec", "-T", "crdb", "cat", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    return completed.stdout.strip()


def _try_read_container_file(path: str) -> str | None:
    completed = subprocess.run(
        ["docker", "compose", "exec", "-T", "crdb", "cat", path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _inspect_container_cgroup() -> dict[str, Any]:
    maximum = _read_container_file("/sys/fs/cgroup/memory.max")
    events_text = _read_container_file("/sys/fs/cgroup/memory.events")
    events: dict[str, int] = {}
    for line in events_text.splitlines():
        parts = line.split()
        if len(parts) != 2 or parts[0] in events:
            raise RuntimeError("capacity container memory.events is malformed")
        events[parts[0]] = _parse_nonnegative_integer(parts[1], name=parts[0])
    if not REQUIRED_EVENT_KEYS.issubset(events):
        raise RuntimeError("capacity container memory.events omits pressure counters")
    return {
        "version": 2,
        "memory_max": (
            "max"
            if maximum == "max"
            else _parse_nonnegative_integer(maximum, name="container memory.max")
        ),
        "memory_current_bytes": _parse_nonnegative_integer(
            _read_container_file("/sys/fs/cgroup/memory.current"),
            name="container memory.current",
        ),
        "memory_peak_bytes": _parse_nonnegative_integer(
            _read_container_file("/sys/fs/cgroup/memory.peak"),
            name="container memory.peak",
        ),
        "events": events,
    }


def _monitor(args: argparse.Namespace) -> int:
    _validate_invocation(args.source_revision, args.mode, args.execution_id, args.compose_project)
    for path in (args.baseline, args.samples, args.ready, args.stop):
        if path.exists():
            raise FileExistsError(f"refusing stale capacity runtime path: {path.name}")
    directory = _current_cgroup_directory()
    cpu_directory = _current_cgroup_leaf()
    baseline_snapshot = _snapshot(directory, cpu_directory=cpu_directory)
    if (
        baseline_snapshot["memory_max_bytes"] != EXPECTED_RUNNER_MEMORY_BYTES
        or baseline_snapshot["cpu_quota_us"] != EXPECTED_CPU_QUOTA_US
        or baseline_snapshot["cpu_period_us"] != EXPECTED_CPU_PERIOD_US
    ):
        raise RuntimeError("capacity runner does not have the reviewed resource envelope")
    baseline = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "source_revision": args.source_revision,
        "mode": args.mode,
        "execution_id": args.execution_id,
        "compose_project": args.compose_project,
        "configured": _configured_envelope(),
        "cgroup": baseline_snapshot,
    }
    _write_json(args.baseline, baseline)
    args.ready.parent.mkdir(parents=True, exist_ok=True)
    args.ready.write_text("ready\n")
    started = time.monotonic()
    sampled_peak = baseline_snapshot["memory_current_bytes"]
    sample_count = 0
    process: dict[str, Any] | None = None
    effective_memory: dict[str, int] | None = None
    next_effective_check = 0.0
    error: str | None = None
    try:
        while not args.stop.exists():
            if time.monotonic() - started > MAX_MONITOR_SECONDS:
                raise TimeoutError("capacity runtime monitor exceeded its bounded lifetime")
            snapshot = _snapshot(directory, cpu_directory=cpu_directory)
            if (
                snapshot["identity"] != baseline_snapshot["identity"]
                or snapshot["memory_max_bytes"] != EXPECTED_RUNNER_MEMORY_BYTES
                or snapshot["cpu_quota_us"] != EXPECTED_CPU_QUOTA_US
                or snapshot["cpu_period_us"] != EXPECTED_CPU_PERIOD_US
            ):
                raise RuntimeError("capacity runtime cgroup changed during the workload")
            sampled_peak = max(sampled_peak, snapshot["memory_current_bytes"])
            sample_count += 1
            if process is None:
                process = _inspect_container(args.compose_project)
            if (
                process is not None
                and effective_memory is None
                and time.monotonic() >= next_effective_check
            ):
                effective_memory = _inspect_effective_memory()
                next_effective_check = time.monotonic() + 1
            time.sleep(SAMPLE_INTERVAL_SECONDS)
        snapshot = _snapshot(directory, cpu_directory=cpu_directory)
        sampled_peak = max(sampled_peak, snapshot["memory_current_bytes"])
        sample_count += 1
        if process is None:
            process = _inspect_container(args.compose_project)
        if process is None:
            raise RuntimeError("capacity runtime did not observe the CockroachDB process")
        if effective_memory is None:
            effective_memory = _inspect_effective_memory()
        if effective_memory is None:
            raise RuntimeError("capacity runtime did not observe effective database memory")
        process = {**process, "effective_memory": effective_memory}
        container_cgroup = _inspect_container_cgroup()
    except BaseException as caught:
        error = f"{type(caught).__name__}: {caught}"
    samples = {
        "schema_version": SAMPLES_SCHEMA_VERSION,
        "source_revision": args.source_revision,
        "mode": args.mode,
        "execution_id": args.execution_id,
        "compose_project": args.compose_project,
        "configured": _configured_envelope(),
        "cgroup_identity": baseline_snapshot["identity"],
        "sample_count": sample_count,
        "sampled_peak_bytes": sampled_peak,
        "effective_process": process,
        "container_cgroup": container_cgroup if error is None else None,
        "error": error,
    }
    _write_json(args.samples, samples)
    if error is not None:
        raise RuntimeError(error)
    return 0


def _finalize(args: argparse.Namespace) -> int:
    _validate_invocation(args.source_revision, args.mode, args.execution_id, args.compose_project)
    baseline = _read_json(args.baseline)
    samples = _read_json(args.samples)
    _validate_identity(
        baseline,
        schema=BASELINE_SCHEMA_VERSION,
        source_revision=args.source_revision,
        mode=args.mode,
        execution_id=args.execution_id,
        project=args.compose_project,
    )
    _validate_identity(
        samples,
        schema=SAMPLES_SCHEMA_VERSION,
        source_revision=args.source_revision,
        mode=args.mode,
        execution_id=args.execution_id,
        project=args.compose_project,
    )
    if samples.get("error") is not None or not isinstance(samples.get("effective_process"), dict):
        raise ValueError("capacity runtime samples do not prove the reviewed process")
    baseline_cgroup = baseline.get("cgroup")
    if not isinstance(baseline_cgroup, dict):
        raise ValueError("capacity runtime baseline omits cgroup telemetry")
    directory = _current_cgroup_directory()
    final_snapshot = _snapshot(directory, cpu_directory=_current_cgroup_leaf())
    if (
        baseline_cgroup.get("identity") != final_snapshot["identity"]
        or samples.get("cgroup_identity") != final_snapshot["identity"]
        or baseline_cgroup.get("memory_max_bytes") != EXPECTED_RUNNER_MEMORY_BYTES
        or final_snapshot["memory_max_bytes"] != EXPECTED_RUNNER_MEMORY_BYTES
        or baseline_cgroup.get("cpu_quota_us") != EXPECTED_CPU_QUOTA_US
        or baseline_cgroup.get("cpu_period_us") != EXPECTED_CPU_PERIOD_US
        or final_snapshot["cpu_quota_us"] != EXPECTED_CPU_QUOTA_US
        or final_snapshot["cpu_period_us"] != EXPECTED_CPU_PERIOD_US
    ):
        raise ValueError("capacity runtime final cgroup differs from the baseline")
    before_events = baseline_cgroup.get("events")
    after_events = final_snapshot["events"]
    if not isinstance(before_events, dict) or set(before_events) != set(after_events):
        raise ValueError("capacity runtime pressure counters changed shape")
    deltas: dict[str, int] = {}
    for key, after in after_events.items():
        before = before_events.get(key)
        if type(before) is not int or type(after) is not int or after < before:
            raise ValueError("capacity runtime pressure counters are not monotonic integers")
        deltas[key] = after - before
    runtime = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "source_revision": args.source_revision,
        "mode": args.mode,
        "execution_id": args.execution_id,
        "compose_project": args.compose_project,
        "configured": _configured_envelope(),
        "effective_process": samples["effective_process"],
        "container_cgroup": samples.get("container_cgroup"),
        "cgroup": {
            "version": 2,
            "memory_max_bytes": EXPECTED_RUNNER_MEMORY_BYTES,
            "cpu_quota_us": EXPECTED_CPU_QUOTA_US,
            "cpu_period_us": EXPECTED_CPU_PERIOD_US,
            "memory_current_before_bytes": baseline_cgroup.get("memory_current_bytes"),
            "memory_current_after_bytes": final_snapshot["memory_current_bytes"],
            "kernel_memory_peak_before_bytes": baseline_cgroup.get("kernel_memory_peak_bytes"),
            "kernel_memory_peak_after_bytes": final_snapshot["kernel_memory_peak_bytes"],
            "sample_count": samples.get("sample_count"),
            "sampled_peak_bytes": samples.get("sampled_peak_bytes"),
            "events_before": before_events,
            "events_after": after_events,
            "event_deltas": deltas,
            "pressure_events_zero": all(delta == 0 for delta in deltas.values()),
        },
    }
    _write_json(args.output, runtime)
    return 0


def _bind_manifest(args: argparse.Namespace) -> int:
    manifest = _read_json(args.manifest)
    runtime = _read_json(args.runtime)
    infrastructure = _read_json(args.infrastructure_cleanup)
    expected_existing = {
        "index-qualification.json",
        "capacity-report.json",
        "cleanup.json",
    }
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("schema_version") != CAPACITY_SCHEMA_VERSION
        or manifest.get("source_revision") != args.source_revision
        or manifest.get("execution_id") != args.execution_id
        or manifest.get("mode") != "qualification"
        or manifest.get("kind") != "capacity_artifact_manifest"
        or not isinstance(artifacts, dict)
        or set(artifacts) != expected_existing
        or runtime.get("source_revision") != args.source_revision
        or runtime.get("execution_id") != args.execution_id
        or runtime.get("mode") != "qualification"
        or infrastructure.get("source_revision") != args.source_revision
        or infrastructure.get("execution_id") != args.execution_id
        or infrastructure.get("mode") != "qualification"
    ):
        raise ValueError("capacity manifest cannot bind the runtime cleanup artifacts")
    manifest["artifacts"] = {
        **artifacts,
        args.runtime.name: _sha256(args.runtime),
        args.infrastructure_cleanup.name: _sha256(args.infrastructure_cleanup),
    }
    _write_json(args.manifest, manifest)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    monitor = subparsers.add_parser("monitor")
    monitor.add_argument("--source-revision", required=True)
    monitor.add_argument("--mode", choices=sorted(MODES), required=True)
    monitor.add_argument("--execution-id", required=True)
    monitor.add_argument("--compose-project", required=True)
    monitor.add_argument("--baseline", type=Path, required=True)
    monitor.add_argument("--samples", type=Path, required=True)
    monitor.add_argument("--ready", type=Path, required=True)
    monitor.add_argument("--stop", type=Path, required=True)
    monitor.set_defaults(handler=_monitor)

    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--source-revision", required=True)
    finalize.add_argument("--mode", choices=sorted(MODES), required=True)
    finalize.add_argument("--execution-id", required=True)
    finalize.add_argument("--compose-project", required=True)
    finalize.add_argument("--baseline", type=Path, required=True)
    finalize.add_argument("--samples", type=Path, required=True)
    finalize.add_argument("--output", type=Path, required=True)
    finalize.set_defaults(handler=_finalize)

    bind = subparsers.add_parser("bind-manifest")
    bind.add_argument("--source-revision", required=True)
    bind.add_argument("--execution-id", required=True)
    bind.add_argument("--manifest", type=Path, required=True)
    bind.add_argument("--runtime", type=Path, required=True)
    bind.add_argument("--infrastructure-cleanup", type=Path, required=True)
    bind.set_defaults(handler=_bind_manifest)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
