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
BASELINE_SCHEMA_VERSION = "hindsight.capacity_runtime_baseline.v3"
SAMPLES_SCHEMA_VERSION = "hindsight.capacity_runtime_samples.v3"
RUNTIME_SCHEMA_VERSION = "hindsight.capacity_runtime.v3"
CAPACITY_SCHEMA_VERSION = "hindsight.capacity_qualification.v6"
MODES = frozenset({"diagnostic", "qualification"})
EXPECTED_IMAGE_DIGEST = "sha256:53f2dea6f5a666551f404bf6c341bde6595964cf786f24ade7d85249ccedecc7"
EXPECTED_IMAGE = f"cockroachdb/cockroach@{EXPECTED_IMAGE_DIGEST}"
EXPECTED_IMAGE_ID = EXPECTED_IMAGE_DIGEST
EXPECTED_IMAGE_PLATFORM = "linux/amd64"
EXPECTED_EXECUTION_TOPOLOGY = "owner_runner_sibling_dind_capacity_cgroup_v2"
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
EXPECTED_BOUNDARY_MEMORY_BYTES = 4 * 1024**3
EXPECTED_BOUNDARY_SWAP_MAX = "max"
EXPECTED_CPU_QUOTA_US = 150_000
EXPECTED_CPU_PERIOD_US = 100_000
EXPECTED_GO_MAX_PROCS = 2
EXPECTED_GOMAXPROCS_ENV = f"GOMAXPROCS={EXPECTED_GO_MAX_PROCS}"
EXPECTED_MEMORY_BYTES = {
    "store": 2 * 1024**3,
    "cache": 128 * 1024**2,
    "sql": 128 * 1024**2,
    "tsdb": 64 * 1024**2,
    "go": 3 * 1024**3,
}
REQUIRED_EVENT_KEYS = frozenset({"low", "high", "max", "oom", "oom_kill"})
PROBE_EVENT_KEYS = ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")
SOURCE_PATTERN = re.compile(r"[0-9a-f]{40}")
EXECUTION_ID_PATTERN = re.compile(r"capacity_[0-9]+_1_(diagnostic|qualification)")
COMPOSE_PROJECT_PATTERN = re.compile(r"hindsight_capacity_[0-9]+_[0-9]+_(diagnostic|qualification)")
CONTAINER_ID_PATTERN = re.compile(r"[0-9a-f]{64}")
NOMINAL_SAMPLE_SLEEP_SECONDS = 0.25
UPTIME_QUANTUM_NS = 10_000_000
MAX_REAL_SAMPLE_GAP_SECONDS = 1.0
MAX_RECORDED_SAMPLE_GAP_NS = 990_000_000
MAX_MONITOR_SECONDS = 1_600
DOCKER_COMMAND_TIMEOUT_SECONDS = 15
PROBE_RECORD_PREFIX = "hindsight.capacity_cgroup_sample.v2"
PROBE_MAX_SAMPLES = 7_200
PROBE_MAX_LOG_BYTES = 2 * 1024**2
PROBE_MEMORY_BYTES = 32 * 1024**2
PROBE_NANO_CPUS = 500_000_000
PROBE_PIDS_LIMIT = 16
PROBE_LABEL_ROLE = "runtime-pressure-probe"
PROBE_LOG_CONFIG = {
    "Type": "json-file",
    "Config": {"max-file": "1", "max-size": "4m"},
}
PROBE_EMIT_FUNCTION = r"""
emit_sample() {
  sequence="$1"
  IFS=' ' read -r monotonic_seconds _ < /proc/uptime
  set -- $(stat -c '%d %i' /sys/fs/cgroup)
  test "$#" -eq 2
  device="$1"
  inode="$2"
  IFS= read -r memory_max < /sys/fs/cgroup/memory.max
  IFS= read -r memory_swap_max < /sys/fs/cgroup/memory.swap.max
  IFS= read -r memory_current < /sys/fs/cgroup/memory.current
  IFS= read -r memory_peak < /sys/fs/cgroup/memory.peak
  IFS= read -r memory_swap_current < /sys/fs/cgroup/memory.swap.current
  IFS=' ' read -r cpu_quota cpu_period cpu_extra < /sys/fs/cgroup/cpu.max
  test -n "$cpu_quota" && test -n "$cpu_period" && test -z "$cpu_extra"
  swaps_active=-1
  while IFS= read -r _; do
    swaps_active=$((swaps_active + 1))
  done < /proc/swaps
  test "$swaps_active" -ge 0
  low=""
  high=""
  max=""
  oom=""
  oom_kill=""
  oom_group_kill=""
  event_count=0
  while read -r event value; do
    case "$event" in
      low) test -z "$low"; low="$value" ;;
      high) test -z "$high"; high="$value" ;;
      max) test -z "$max"; max="$value" ;;
      oom) test -z "$oom"; oom="$value" ;;
      oom_kill) test -z "$oom_kill"; oom_kill="$value" ;;
      oom_group_kill) test -z "$oom_group_kill"; oom_group_kill="$value" ;;
      *) exit 65 ;;
    esac
    event_count=$((event_count + 1))
  done < /sys/fs/cgroup/memory.events
  test "$event_count" -eq 6
  test -n "$low" && test -n "$high" && test -n "$max"
  test -n "$oom" && test -n "$oom_kill" && test -n "$oom_group_kill"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    'hindsight.capacity_cgroup_sample.v2' "$sequence" "$monotonic_seconds" "$device" "$inode" \
    "$memory_max" "$memory_swap_max" "$memory_current" "$memory_peak" \
    "$memory_swap_current" "$cpu_quota" "$cpu_period" "$swaps_active" \
    "$low" "$high" "$max" "$oom" "$oom_kill" "$oom_group_kill"
}
""".strip()
PROBE_LOOP_SCRIPT = (
    PROBE_EMIT_FUNCTION
    + r"""
sample_sequence=0
trap 'emit_sample "$sample_sequence"; exit 0' TERM INT
while [ "$sample_sequence" -lt 7200 ]; do
  emit_sample "$sample_sequence"
  sample_sequence=$((sample_sequence + 1))
  sleep 0.25
done
exit 124
"""
)
PROBE_SNAPSHOT_SCRIPT = PROBE_EMIT_FUNCTION + "\nemit_sample 0\n"


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


def _parse_monotonic_seconds(raw: str, *, name: str) -> int:
    matched = re.fullmatch(r"(0|[1-9][0-9]*)\.([0-9]{2})", raw)
    if matched is None:
        raise ValueError(f"{name} is not canonical kernel uptime")
    return (
        int(matched.group(1)) * 1_000_000_000
        + int(matched.group(2)) * UPTIME_QUANTUM_NS
    )


def _validate_uptime_ns(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0 or value % UPTIME_QUANTUM_NS != 0:
        raise RuntimeError(
            f"{name} must be a positive {UPTIME_QUANTUM_NS}-nanosecond-quantized "
            f"uptime: value_ns={value!r}"
        )
    return value


def _read_monotonic_uptime_ns() -> int:
    parts = Path("/proc/uptime").read_text().split()
    if len(parts) != 2:
        raise RuntimeError("host monotonic uptime is malformed")
    return _parse_monotonic_seconds(parts[0], name="host monotonic uptime")


def _configured_envelope() -> dict[str, Any]:
    return {
        "image": EXPECTED_IMAGE,
        "image_id": EXPECTED_IMAGE_ID,
        "image_platform": EXPECTED_IMAGE_PLATFORM,
        "execution_topology": EXPECTED_EXECUTION_TOPOLOGY,
        "go_max_procs": EXPECTED_GO_MAX_PROCS,
        "start_args": list(EXPECTED_START_ARGS),
        "capacity_boundary": {
            "cgroup_version": 2,
            "memory_max_bytes": EXPECTED_BOUNDARY_MEMORY_BYTES,
            "memory_swap_max": EXPECTED_BOUNDARY_SWAP_MAX,
            "swap_devices": 0,
            "cpu_quota_us": EXPECTED_CPU_QUOTA_US,
            "cpu_period_us": EXPECTED_CPU_PERIOD_US,
        },
        "telemetry_probe": {
            "image": EXPECTED_IMAGE,
            "image_id": EXPECTED_IMAGE_ID,
            "image_platform": EXPECTED_IMAGE_PLATFORM,
            "cgroup_namespace": "host",
            "network": "none",
            "read_only": True,
            "user": "65534:65534",
            "cap_drop": ["ALL"],
            "no_new_privileges": True,
            "privileged": False,
            "workspace_mounts": 0,
            "pids_limit": PROBE_PIDS_LIMIT,
            "memory_bytes": PROBE_MEMORY_BYTES,
            "nano_cpus": PROBE_NANO_CPUS,
            "nominal_sample_sleep_seconds": NOMINAL_SAMPLE_SLEEP_SECONDS,
            "maximum_sample_gap_seconds": MAX_REAL_SAMPLE_GAP_SECONDS,
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
    if os.environ.get("COCKROACH_IMAGE_ID") != EXPECTED_IMAGE_ID:
        raise ValueError("capacity runtime CockroachDB image ID differs from the reviewed image")
    if os.environ.get("COCKROACH_START_ARGS") != EXPECTED_START_ARGS_TEXT:
        raise ValueError("capacity runtime memory arguments differ from the reviewed envelope")
    if os.environ.get("COCKROACH_GOMAXPROCS") != str(EXPECTED_GO_MAX_PROCS):
        raise ValueError("capacity runtime Go processor budget differs from the reviewed envelope")


def _probe_name(project: str) -> str:
    return f"{project}_runtime_probe"


def _probe_labels(execution_id: str, project: str) -> dict[str, str]:
    return {
        "hindsight.capacity.execution_id": execution_id,
        "hindsight.capacity.compose_project": project,
        "hindsight.capacity.role": PROBE_LABEL_ROLE,
    }


def _inspect_expected_image() -> None:
    completed = subprocess.run(
        ["docker", "image", "inspect", EXPECTED_IMAGE],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    payload = json.loads(completed.stdout)
    row = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
    if (
        not isinstance(row, dict)
        or row.get("Id") != EXPECTED_IMAGE_ID
        or row.get("Os") != "linux"
        or row.get("Architecture") != "amd64"
        or EXPECTED_IMAGE not in row.get("RepoDigests", [])
    ):
        raise RuntimeError("capacity image differs from the reviewed linux/amd64 digest")


def _parse_probe_record(line: str) -> tuple[int, dict[str, Any]]:
    parts = line.rstrip("\n").split("\t")
    if len(parts) != 19 or parts[0] != PROBE_RECORD_PREFIX:
        raise ValueError("capacity cgroup probe record is malformed")
    sequence = _parse_nonnegative_integer(parts[1], name="probe sequence")
    memory_swap_max: str | int
    if parts[6] == "max":
        memory_swap_max = "max"
    else:
        memory_swap_max = _parse_nonnegative_integer(
            parts[6], name="probe memory.swap.max"
        )
    snapshot = {
        "monotonic_ns": _parse_monotonic_seconds(
            parts[2], name="probe monotonic uptime"
        ),
        "identity": {
            "device": _parse_nonnegative_integer(parts[3], name="probe cgroup device"),
            "inode": _parse_nonnegative_integer(parts[4], name="probe cgroup inode"),
        },
        "memory_max_bytes": _parse_nonnegative_integer(
            parts[5], name="probe memory.max"
        ),
        "memory_swap_max": memory_swap_max,
        "memory_current_bytes": _parse_nonnegative_integer(
            parts[7], name="probe memory.current"
        ),
        "kernel_memory_peak_bytes": _parse_nonnegative_integer(
            parts[8], name="probe memory.peak"
        ),
        "memory_swap_current_bytes": _parse_nonnegative_integer(
            parts[9], name="probe memory.swap.current"
        ),
        "cpu_quota_us": _parse_nonnegative_integer(parts[10], name="probe cpu quota"),
        "cpu_period_us": _parse_nonnegative_integer(parts[11], name="probe cpu period"),
        "swap_devices": _parse_nonnegative_integer(parts[12], name="probe swap devices"),
        "events": {
            key: _parse_nonnegative_integer(parts[13 + offset], name=f"probe {key}")
            for offset, key in enumerate(PROBE_EVENT_KEYS)
        },
    }
    _validate_probe_snapshot(snapshot)
    return sequence, snapshot


def _validate_probe_snapshot(snapshot: dict[str, Any]) -> None:
    if (
        type(snapshot.get("monotonic_ns")) is not int
        or snapshot["monotonic_ns"] <= 0
        or snapshot["monotonic_ns"] % UPTIME_QUANTUM_NS != 0
        or snapshot.get("memory_max_bytes") != EXPECTED_BOUNDARY_MEMORY_BYTES
        or snapshot.get("memory_swap_max") != EXPECTED_BOUNDARY_SWAP_MAX
        or snapshot.get("memory_swap_current_bytes") != 0
        or snapshot.get("swap_devices") != 0
        or snapshot.get("cpu_quota_us") != EXPECTED_CPU_QUOTA_US
        or snapshot.get("cpu_period_us") != EXPECTED_CPU_PERIOD_US
    ):
        raise RuntimeError("capacity probe does not observe the reviewed DinD boundary")
    current = snapshot.get("memory_current_bytes")
    peak = snapshot.get("kernel_memory_peak_bytes")
    identity = snapshot.get("identity")
    events = snapshot.get("events")
    if (
        type(current) is not int
        or not 0 <= current <= EXPECTED_BOUNDARY_MEMORY_BYTES
        or type(peak) is not int
        or peak < current
        or not isinstance(identity, dict)
        or set(identity) != {"device", "inode"}
        or any(type(value) is not int or value <= 0 for value in identity.values())
        or not isinstance(events, dict)
        or tuple(events) != PROBE_EVENT_KEYS
        or any(type(value) is not int or value < 0 for value in events.values())
    ):
        raise RuntimeError("capacity probe returned invalid DinD cgroup telemetry")


def _validate_probe_series(
    records: list[tuple[int, dict[str, Any]]],
    *,
    final_snapshot: dict[str, Any] | None = None,
) -> tuple[int, int]:
    if not records:
        raise RuntimeError("capacity cgroup probe emitted no samples")
    samples: list[tuple[int | str, dict[str, Any]]] = list(records)
    if final_snapshot is not None:
        samples.append(("final_snapshot", final_snapshot))
    for sequence, snapshot in samples:
        _validate_uptime_ns(
            snapshot.get("monotonic_ns"), name=f"probe sequence {sequence} monotonic uptime"
        )
    gaps: list[int] = []
    for (previous_sequence, previous), (current_sequence, current) in zip(
        samples, samples[1:], strict=False
    ):
        gap = current["monotonic_ns"] - previous["monotonic_ns"]
        if gap <= 0 or gap > MAX_RECORDED_SAMPLE_GAP_NS:
            raise RuntimeError(
                "capacity cgroup probe sampling cadence is invalid between "
                f"sequences {previous_sequence} and {current_sequence}: gap_ns={gap}"
            )
        gaps.append(gap)
        if current["identity"] != previous["identity"]:
            raise RuntimeError(
                "capacity DinD cgroup identity changed during sampling between "
                f"sequences {previous_sequence} and {current_sequence}"
            )
        if current["kernel_memory_peak_bytes"] < previous["kernel_memory_peak_bytes"]:
            raise RuntimeError(
                "capacity cgroup memory peak is not monotonic between "
                f"sequences {previous_sequence} and {current_sequence}"
            )
        if any(
            current["events"][key] < previous["events"][key]
            for key in PROBE_EVENT_KEYS
        ):
            raise RuntimeError(
                "capacity cgroup pressure counters are not monotonic between "
                f"sequences {previous_sequence} and {current_sequence}"
            )
    return (
        max(gaps, default=0),
        samples[-1][1]["monotonic_ns"] - samples[0][1]["monotonic_ns"],
    )


def _validate_boundary_bridge(
    *, name: str, observed_monotonic_ns: object, sample_sequence: int, sample_monotonic_ns: object
) -> int:
    observed = _validate_uptime_ns(
        observed_monotonic_ns, name=f"{name} observed monotonic uptime"
    )
    sampled = _validate_uptime_ns(
        sample_monotonic_ns,
        name=f"{name} sample sequence {sample_sequence} monotonic uptime",
    )
    gap = sampled - observed
    if gap < 0 or gap > MAX_RECORDED_SAMPLE_GAP_NS:
        raise RuntimeError(
            f"capacity probe {name} bridge is invalid at sequence "
            f"{sample_sequence}: gap_ns={gap}"
        )
    return gap


def _probe_run_command(execution_id: str, project: str) -> list[str]:
    labels = _probe_labels(execution_id, project)
    command = [
        "docker",
        "run",
        "--detach",
        "--rm",
        "--pull",
        "never",
        "--name",
        _probe_name(project),
        "--network",
        "none",
        "--read-only",
        "--user",
        "65534:65534",
        "--pids-limit",
        str(PROBE_PIDS_LIMIT),
        "--memory",
        str(PROBE_MEMORY_BYTES),
        "--memory-swap",
        str(PROBE_MEMORY_BYTES),
        "--cpus",
        "0.50",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cgroupns",
        "host",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=4m",
        "--log-opt",
        "max-file=1",
    ]
    for key, value in sorted(labels.items()):
        command.extend(("--label", f"{key}={value}"))
    command.extend(("--entrypoint", "/bin/sh", EXPECTED_IMAGE, "-ec", PROBE_LOOP_SCRIPT))
    return command


def _inspect_probe(execution_id: str, project: str, *, require_running: bool) -> dict[str, Any]:
    name = _probe_name(project)
    completed = subprocess.run(
        ["docker", "inspect", name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    payload = json.loads(completed.stdout)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("capacity cgroup probe inspection is malformed")
    row = payload[0]
    config = row.get("Config")
    host = row.get("HostConfig")
    state = row.get("State")
    labels = config.get("Labels") if isinstance(config, dict) else None
    security = host.get("SecurityOpt") if isinstance(host, dict) else None
    if (
        row.get("Name") != f"/{name}"
        or CONTAINER_ID_PATTERN.fullmatch(str(row.get("Id"))) is None
        or not isinstance(config, dict)
        or not isinstance(host, dict)
        or not isinstance(state, dict)
        or config.get("Image") != EXPECTED_IMAGE
        or row.get("Image") != EXPECTED_IMAGE_ID
        or config.get("User") != "65534:65534"
        or config.get("Entrypoint") != ["/bin/sh"]
        or config.get("Cmd") != ["-ec", PROBE_LOOP_SCRIPT]
        or not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in _probe_labels(execution_id, project).items())
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or host.get("CgroupnsMode") != "host"
        or host.get("CapDrop") != ["ALL"]
        or not isinstance(security, list)
        or set(security) != {"no-new-privileges"}
        or host.get("PidsLimit") != PROBE_PIDS_LIMIT
        or host.get("Memory") != PROBE_MEMORY_BYTES
        or host.get("MemorySwap") != PROBE_MEMORY_BYTES
        or host.get("NanoCpus") != PROBE_NANO_CPUS
        or host.get("AutoRemove") is not True
        or host.get("LogConfig") != PROBE_LOG_CONFIG
        or row.get("Mounts") != []
        or (require_running and state.get("Running") is not True)
    ):
        raise RuntimeError("capacity cgroup probe differs from the reviewed security profile")
    return row


def _probe_records(name: str, *, tail: int | None = None) -> list[tuple[int, dict[str, Any]]]:
    command = ["docker", "logs"]
    if tail is not None:
        command.extend(("--tail", str(tail)))
    command.append(name)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    if len(completed.stdout.encode()) > PROBE_MAX_LOG_BYTES:
        raise RuntimeError("capacity cgroup probe log exceeds its bound")
    lines = [line for line in completed.stdout.splitlines() if line]
    if len(lines) > PROBE_MAX_SAMPLES:
        raise RuntimeError("capacity cgroup probe emitted too many samples")
    records = [_parse_probe_record(line) for line in lines]
    if tail is None and [sequence for sequence, _snapshot in records] != list(
        range(len(records))
    ):
        raise RuntimeError("capacity cgroup probe sample sequence is discontinuous")
    return records


def _start_probe(execution_id: str, project: str) -> tuple[str, dict[str, Any]]:
    name = _probe_name(project)
    _inspect_expected_image()
    existing = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name=^/{name}$"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    if existing.stdout.strip():
        raise RuntimeError("capacity cgroup probe name is already in use")
    started = subprocess.run(
        _probe_run_command(execution_id, project),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    identifier = started.stdout.strip()
    if started.returncode != 0 or CONTAINER_ID_PATTERN.fullmatch(identifier) is None:
        raise RuntimeError("capacity cgroup probe could not start")
    row = _inspect_probe(execution_id, project, require_running=True)
    if row.get("Id") != identifier:
        raise RuntimeError("capacity cgroup probe identity changed during startup")
    deadline = time.monotonic() + DOCKER_COMMAND_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        records = _probe_records(name)
        if records:
            if records[0][0] != 0:
                raise RuntimeError("capacity cgroup probe baseline sequence is invalid")
            return identifier, records[0][1]
        time.sleep(0.1)
    raise RuntimeError("capacity cgroup probe did not emit its baseline")


def _final_probe_snapshot(execution_id: str, project: str, identifier: str) -> dict[str, Any]:
    row = _inspect_probe(execution_id, project, require_running=True)
    if row.get("Id") != identifier:
        raise RuntimeError("capacity cgroup probe identity changed before finalization")
    completed = subprocess.run(
        ["docker", "exec", _probe_name(project), "/bin/sh", "-ec", PROBE_SNAPSHOT_SCRIPT],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    lines = [line for line in completed.stdout.splitlines() if line]
    if len(lines) != 1:
        raise RuntimeError("capacity cgroup probe final snapshot is malformed")
    _sequence, snapshot = _parse_probe_record(lines[0])
    return snapshot


def _remove_probe(execution_id: str, project: str) -> None:
    name = _probe_name(project)
    listed = subprocess.run(
        ["docker", "ps", "-aq", "--no-trunc", "--filter", f"name=^/{name}$"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    identifiers = [line for line in listed.stdout.splitlines() if line]
    if not identifiers:
        return
    if len(identifiers) != 1 or CONTAINER_ID_PATTERN.fullmatch(identifiers[0]) is None:
        raise RuntimeError("capacity cgroup probe cleanup resolved an invalid container")
    inspected = subprocess.run(
        ["docker", "inspect", identifiers[0]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    payload = json.loads(inspected.stdout)
    row = payload[0] if isinstance(payload, list) and len(payload) == 1 else None
    labels = row.get("Config", {}).get("Labels") if isinstance(row, dict) else None
    if (
        not isinstance(row, dict)
        or row.get("Name") != f"/{name}"
        or not isinstance(labels, dict)
        or any(labels.get(key) != value for key, value in _probe_labels(execution_id, project).items())
    ):
        raise RuntimeError("capacity cgroup probe cleanup identity is invalid")
    subprocess.run(
        ["docker", "rm", "-f", identifiers[0]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    verified = subprocess.run(
        ["docker", "ps", "-aq", "--no-trunc", "--filter", f"name=^/{name}$"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
    )
    if verified.stdout.strip():
        raise RuntimeError("capacity cgroup probe remains after cleanup")


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
    host = row.get("HostConfig")
    state = row.get("State")
    if (
        not isinstance(config, dict)
        or not isinstance(host, dict)
        or not isinstance(state, dict)
    ):
        raise RuntimeError("capacity runtime Docker inspection is incomplete")
    labels = config.get("Labels")
    configured_go_max_procs = _go_max_procs_from_environment(
        config.get("Env"), source="Docker configuration"
    )
    process = {
        "path": row.get("Path"),
        "args": row.get("Args"),
        "configured_command": config.get("Cmd"),
        "image": config.get("Image"),
        "cgroup_namespace": host.get("CgroupnsMode"),
        "compose_project": labels.get("com.docker.compose.project")
        if isinstance(labels, dict)
        else None,
        "compose_service": labels.get("com.docker.compose.service")
        if isinstance(labels, dict)
        else None,
        "running": state.get("Running"),
        "configured_go_max_procs": configured_go_max_procs,
    }
    if (
        process["path"] != "/cockroach/cockroach.sh"
        or process["args"] != list(EXPECTED_PROCESS_ARGS)
        or process["configured_command"] != list(EXPECTED_PROCESS_ARGS)
        or process["image"] != EXPECTED_IMAGE
        or row.get("Image") != EXPECTED_IMAGE_ID
        or process["cgroup_namespace"] != "private"
        or process["compose_project"] != project
        or process["compose_service"] != "crdb"
        or process["running"] is not True
    ):
        raise RuntimeError("capacity CockroachDB process differs from the reviewed envelope")
    health = state.get("Health")
    health_status = health.get("Status") if isinstance(health, dict) else None
    if (
        not isinstance(health, dict)
        or not isinstance(health_status, str)
        or health_status not in {"starting", "healthy"}
    ):
        raise RuntimeError("capacity CockroachDB Docker health is invalid")
    pid_raw = _try_read_container_file("/cockroach/server_pid")
    if pid_raw is None:
        if health_status == "starting":
            return None
        raise RuntimeError("healthy capacity CockroachDB server PID is missing")
    pid = _parse_nonnegative_integer(pid_raw, name="CockroachDB server PID")
    if pid == 0:
        raise RuntimeError("capacity CockroachDB server PID is invalid")
    live_raw = _try_read_container_file(f"/proc/{pid}/cmdline")
    if not live_raw:
        if health_status == "starting":
            return None
        raise RuntimeError("healthy capacity CockroachDB live argv is missing")
    live_argv = [part for part in live_raw.split("\0") if part]
    if health_status == "starting":
        return None
    if live_argv != list(EXPECTED_LIVE_PROCESS_ARGS):
        raise RuntimeError("capacity CockroachDB live argv differs from the reviewed envelope")
    live_environment_raw = _try_read_container_file(f"/proc/{pid}/environ")
    if not live_environment_raw:
        raise RuntimeError("healthy capacity CockroachDB live environment is missing")
    live_go_max_procs = _go_max_procs_from_environment(
        [part for part in live_environment_raw.split("\0") if part],
        source="live CockroachDB process",
    )
    process["live_argv"] = live_argv
    process["live_go_max_procs"] = live_go_max_procs
    return process


def _go_max_procs_from_environment(entries: object, *, source: str) -> int:
    if not isinstance(entries, list) or any(
        not isinstance(entry, str) for entry in entries
    ):
        raise RuntimeError(f"capacity {source} environment is malformed")
    matches = [entry for entry in entries if entry.partition("=")[0] == "GOMAXPROCS"]
    if len(matches) != 1 or matches[0] != EXPECTED_GOMAXPROCS_ENV:
        raise RuntimeError(
            f"capacity {source} must contain exactly {EXPECTED_GOMAXPROCS_ENV}"
        )
    return EXPECTED_GO_MAX_PROCS


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
    probe_identifier, baseline_snapshot = _start_probe(
        args.execution_id, args.compose_project
    )
    baseline = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "source_revision": args.source_revision,
        "mode": args.mode,
        "execution_id": args.execution_id,
        "compose_project": args.compose_project,
        "configured": _configured_envelope(),
        "probe_container_id": probe_identifier,
        "baseline_sequence": 0,
        "cgroup": baseline_snapshot,
    }
    _write_json(args.baseline, baseline)
    args.ready.parent.mkdir(parents=True, exist_ok=True)
    args.ready.write_text("ready\n")
    started = time.monotonic()
    process: dict[str, Any] | None = None
    effective_memory: dict[str, int] | None = None
    next_effective_check = 0.0
    next_probe_check = 0.0
    workload_records: list[tuple[int, dict[str, Any]]] = []
    workload_stop_observed_monotonic_ns: int | None = None
    workload_observed_max_sample_gap_ns: int | None = None
    workload_sampling_elapsed_ns: int | None = None
    container_cgroup: dict[str, Any] | None = None
    error: str | None = None
    try:
        while not args.stop.exists():
            if time.monotonic() - started > MAX_MONITOR_SECONDS:
                raise TimeoutError("capacity runtime monitor exceeded its bounded lifetime")
            if time.monotonic() >= next_probe_check:
                row = _inspect_probe(
                    args.execution_id, args.compose_project, require_running=True
                )
                if row.get("Id") != probe_identifier:
                    raise RuntimeError("capacity cgroup probe identity changed during the workload")
                next_probe_check = time.monotonic() + 10
            if process is None:
                process = _inspect_container(args.compose_project)
            if (
                process is not None
                and effective_memory is None
                and time.monotonic() >= next_effective_check
            ):
                effective_memory = _inspect_effective_memory()
                next_effective_check = time.monotonic() + 1
            time.sleep(NOMINAL_SAMPLE_SLEEP_SECONDS)
        workload_stop_observed_monotonic_ns = _read_monotonic_uptime_ns()
        row = _inspect_probe(args.execution_id, args.compose_project, require_running=True)
        if row.get("Id") != probe_identifier:
            raise RuntimeError("capacity cgroup probe identity changed during the workload")
        record_deadline = time.monotonic() + DOCKER_COMMAND_TIMEOUT_SECONDS
        while True:
            workload_records = _probe_records(_probe_name(args.compose_project))
            if (
                workload_records
                and workload_records[-1][1]["monotonic_ns"]
                >= workload_stop_observed_monotonic_ns
            ):
                break
            if time.monotonic() >= record_deadline:
                if workload_records:
                    _validate_boundary_bridge(
                        name="workload boundary",
                        observed_monotonic_ns=workload_stop_observed_monotonic_ns,
                        sample_sequence=workload_records[-1][0],
                        sample_monotonic_ns=workload_records[-1][1]["monotonic_ns"],
                    )
                raise RuntimeError("capacity probe did not sample the workload stop boundary")
            time.sleep(0.05)
        workload_observed_max_sample_gap_ns, workload_sampling_elapsed_ns = (
            _validate_probe_series(workload_records)
        )
        if workload_records[0] != (0, baseline_snapshot):
            raise RuntimeError("capacity probe workload boundary is invalid")
        _validate_boundary_bridge(
            name="workload boundary",
            observed_monotonic_ns=workload_stop_observed_monotonic_ns,
            sample_sequence=workload_records[-1][0],
            sample_monotonic_ns=workload_records[-1][1]["monotonic_ns"],
        )
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
        "probe_container_id": probe_identifier,
        "baseline_sequence": 0,
        "cgroup_identity": baseline_snapshot["identity"],
        "workload_stop_observed_monotonic_ns": workload_stop_observed_monotonic_ns,
        "workload_sample_count": len(workload_records),
        "workload_last_sequence": (
            workload_records[-1][0] if workload_records else None
        ),
        "workload_sampled_peak_bytes": (
            max(snapshot["memory_current_bytes"] for _sequence, snapshot in workload_records)
            if workload_records
            else None
        ),
        "workload_last_monotonic_ns": (
            workload_records[-1][1]["monotonic_ns"] if workload_records else None
        ),
        "workload_observed_max_sample_gap_ns": workload_observed_max_sample_gap_ns,
        "workload_sampling_elapsed_ns": workload_sampling_elapsed_ns,
        "effective_process": process,
        "container_cgroup": container_cgroup,
        "error": error,
    }
    _write_json(args.samples, samples)
    if error is not None:
        raise RuntimeError(error)
    return 0


def _finalize(args: argparse.Namespace) -> int:
    _validate_invocation(args.source_revision, args.mode, args.execution_id, args.compose_project)
    try:
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
        if samples.get("error") is not None or not isinstance(
            samples.get("effective_process"), dict
        ):
            raise ValueError("capacity runtime samples do not prove the reviewed process")
        baseline_cgroup = baseline.get("cgroup")
        identifier = baseline.get("probe_container_id")
        if (
            not isinstance(baseline_cgroup, dict)
            or CONTAINER_ID_PATTERN.fullmatch(str(identifier)) is None
            or samples.get("probe_container_id") != identifier
        ):
            raise ValueError("capacity runtime baseline omits probe telemetry")
        post_teardown_observed_monotonic_ns = _read_monotonic_uptime_ns()
        record_deadline = time.monotonic() + DOCKER_COMMAND_TIMEOUT_SECONDS
        while True:
            records = _probe_records(_probe_name(args.compose_project))
            if (
                records
                and records[-1][1]["monotonic_ns"]
                >= post_teardown_observed_monotonic_ns
            ):
                break
            if time.monotonic() >= record_deadline:
                if records:
                    _validate_boundary_bridge(
                        name="post-teardown boundary",
                        observed_monotonic_ns=post_teardown_observed_monotonic_ns,
                        sample_sequence=records[-1][0],
                        sample_monotonic_ns=records[-1][1]["monotonic_ns"],
                    )
                raise RuntimeError("capacity probe did not sample the post-teardown boundary")
            time.sleep(0.05)
        final_snapshot = _final_probe_snapshot(
            args.execution_id, args.compose_project, identifier
        )
        baseline_sequence = baseline.get("baseline_sequence")
        workload_count = samples.get("workload_sample_count")
        workload_last = samples.get("workload_last_sequence")
        workload_last_monotonic_ns = samples.get("workload_last_monotonic_ns")
        workload_stop_monotonic_ns = samples.get(
            "workload_stop_observed_monotonic_ns"
        )
        workload_peak = samples.get("workload_sampled_peak_bytes")
        workload_max_gap_ns = samples.get("workload_observed_max_sample_gap_ns")
        workload_elapsed_ns = samples.get("workload_sampling_elapsed_ns")
        workload_records = records[:workload_count] if type(workload_count) is int else []
        measured_workload_max_gap_ns, measured_workload_elapsed_ns = (
            _validate_probe_series(workload_records)
            if workload_records
            else (None, None)
        )
        observed_max_gap_ns, sampling_elapsed_ns = _validate_probe_series(
            records, final_snapshot=final_snapshot
        )
        if (
            type(workload_last) is int
            and type(workload_last_monotonic_ns) is int
            and type(workload_stop_monotonic_ns) is int
        ):
            _validate_boundary_bridge(
                name="workload boundary",
                observed_monotonic_ns=workload_stop_monotonic_ns,
                sample_sequence=workload_last,
                sample_monotonic_ns=workload_last_monotonic_ns,
            )
        if records:
            _validate_boundary_bridge(
                name="post-teardown boundary",
                observed_monotonic_ns=post_teardown_observed_monotonic_ns,
                sample_sequence=records[-1][0],
                sample_monotonic_ns=records[-1][1]["monotonic_ns"],
            )
        if (
            not records
            or baseline_sequence != 0
            or baseline.get("baseline_sequence") != samples.get("baseline_sequence")
            or records[0] != (baseline_sequence, baseline_cgroup)
            or type(workload_count) is not int
            or workload_count <= 0
            or type(workload_last) is not int
            or workload_last != workload_count - 1
            or type(workload_peak) is not int
            or workload_peak <= 0
            or type(workload_last_monotonic_ns) is not int
            or type(workload_stop_monotonic_ns) is not int
            or type(workload_max_gap_ns) is not int
            or type(workload_elapsed_ns) is not int
            or len(records) <= workload_count
            or records[workload_count - 1][0] != workload_last
            or records[workload_count - 1][1]["monotonic_ns"]
            != workload_last_monotonic_ns
            or not baseline_cgroup["monotonic_ns"]
            <= workload_stop_monotonic_ns
            <= workload_last_monotonic_ns
            or measured_workload_max_gap_ns != workload_max_gap_ns
            or measured_workload_elapsed_ns != workload_elapsed_ns
            or max(
                snapshot["memory_current_bytes"]
                for _sequence, snapshot in workload_records
            )
            != workload_peak
            or final_snapshot["monotonic_ns"] <= workload_last_monotonic_ns
            or not workload_last_monotonic_ns
            < post_teardown_observed_monotonic_ns
            <= records[-1][1]["monotonic_ns"]
            < final_snapshot["monotonic_ns"]
            or baseline_cgroup.get("identity") != final_snapshot["identity"]
            or samples.get("cgroup_identity") != final_snapshot["identity"]
            or any(
                snapshot["identity"] != final_snapshot["identity"]
                for _sequence, snapshot in records
            )
        ):
            raise ValueError("capacity runtime probe continuity is invalid")
        before_events = baseline_cgroup.get("events")
        after_events = final_snapshot["events"]
        if (
            not isinstance(before_events, dict)
            or set(before_events) != set(PROBE_EVENT_KEYS)
            or set(after_events) != set(PROBE_EVENT_KEYS)
        ):
            raise ValueError("capacity runtime pressure counters changed shape")
        deltas: dict[str, int] = {}
        for key, after in after_events.items():
            before = before_events.get(key)
            if type(before) is not int or type(after) is not int or after < before:
                raise ValueError("capacity runtime pressure counters are not monotonic integers")
            deltas[key] = after - before
        sampled_peak = max(
            final_snapshot["memory_current_bytes"],
            *(snapshot["memory_current_bytes"] for _sequence, snapshot in records),
        )
        if sampled_peak < workload_peak:
            raise ValueError("capacity runtime final samples omit the workload peak")
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
                "scope": "sibling_dind_daemon_and_descendants",
                "source": "sandboxed_cgroupns_host_probe",
                "memory_max_bytes": EXPECTED_BOUNDARY_MEMORY_BYTES,
                "memory_swap_max": EXPECTED_BOUNDARY_SWAP_MAX,
                "memory_swap_current_before_bytes": baseline_cgroup.get(
                    "memory_swap_current_bytes"
                ),
                "memory_swap_current_after_bytes": final_snapshot[
                    "memory_swap_current_bytes"
                ],
                "swap_devices_before": baseline_cgroup.get("swap_devices"),
                "swap_devices_after": final_snapshot["swap_devices"],
                "cpu_quota_us": EXPECTED_CPU_QUOTA_US,
                "cpu_period_us": EXPECTED_CPU_PERIOD_US,
                "memory_current_before_bytes": baseline_cgroup.get(
                    "memory_current_bytes"
                ),
                "memory_current_after_bytes": final_snapshot["memory_current_bytes"],
                "kernel_memory_peak_before_bytes": baseline_cgroup.get(
                    "kernel_memory_peak_bytes"
                ),
                "kernel_memory_peak_after_bytes": final_snapshot[
                    "kernel_memory_peak_bytes"
                ],
                "nominal_sample_sleep_seconds": NOMINAL_SAMPLE_SLEEP_SECONDS,
                "maximum_sample_gap_seconds": MAX_REAL_SAMPLE_GAP_SECONDS,
                "observed_max_sample_gap_ns": observed_max_gap_ns,
                "sampling_elapsed_ns": sampling_elapsed_ns,
                "baseline_sequence": baseline_sequence,
                "baseline_monotonic_ns": baseline_cgroup["monotonic_ns"],
                "workload_stop_observed_monotonic_ns": workload_stop_monotonic_ns,
                "workload_last_sequence": workload_last,
                "workload_last_monotonic_ns": workload_last_monotonic_ns,
                "post_teardown_observed_monotonic_ns": (
                    post_teardown_observed_monotonic_ns
                ),
                "post_teardown_sample_monotonic_ns": records[-1][1]["monotonic_ns"],
                "final_snapshot_monotonic_ns": final_snapshot["monotonic_ns"],
                "sample_count": len(records) + 1,
                "sampled_peak_bytes": sampled_peak,
                "events_before": before_events,
                "events_after": after_events,
                "event_deltas": deltas,
                "pressure_events_zero": all(delta == 0 for delta in deltas.values()),
            },
        }
        _write_json(args.output, runtime)
        return 0
    finally:
        _remove_probe(args.execution_id, args.compose_project)


def _cleanup_probe(args: argparse.Namespace) -> int:
    _validate_invocation(args.source_revision, args.mode, args.execution_id, args.compose_project)
    _remove_probe(args.execution_id, args.compose_project)
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

    cleanup = subparsers.add_parser("cleanup-probe")
    cleanup.add_argument("--source-revision", required=True)
    cleanup.add_argument("--mode", choices=sorted(MODES), required=True)
    cleanup.add_argument("--execution-id", required=True)
    cleanup.add_argument("--compose-project", required=True)
    cleanup.set_defaults(handler=_cleanup_probe)

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
