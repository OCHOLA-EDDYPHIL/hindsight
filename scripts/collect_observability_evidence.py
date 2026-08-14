"""Collect bounded, secret-safe hosted alert and trace correlation evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from exercise_alert_delivery import exercise

FULL_SHA = re.compile(r"[0-9a-f]{40}")
ACCOUNT_ID = re.compile(r"[0-9]{12}")
INTEGER = re.compile(r"[1-9][0-9]*")
MAX_LOG_EVENTS = 200
MAX_CANDIDATE_TRACES = 20
TRACE_BATCH_SIZE = 5
MAX_SCAN_BYTES = 256 * 1024 * 1024
MAX_WINDOW_SECONDS = 4 * 60 * 60
COLLECTION_ATTEMPTS = 3
COLLECTION_DELAY_SECONDS = 15
QUERY_POLLS = 20
QUERY_POLL_SECONDS = 3
CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=10,
    retries={"total_max_attempts": 2, "mode": "standard"},
)
SECRET_KEY = re.compile(r"(?i)(authorization|cookie|credential|password|secret|token|api[_-]?key)")
SECRET_VALUE = re.compile(r"(?i)(bearer\s+|api[_-]?key|password|secret|authorization:)")
BROWSER_SAFE_SCALAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}")
EVENT_FIELDS = {
    "api_request": {"event", "status", "tenant_id", "trace_id", "span_id"},
    "run_dispatch": {
        "event",
        "status",
        "command",
        "incident_id",
        "tenant_id",
        "run_id",
        "dispatch_id",
        "dispatch_attempt_id",
        "message_id",
        "trace_id",
        "span_id",
    },
    "worker_record": {
        "event",
        "status",
        "command",
        "message_id",
        "lambda_request_id",
        "tenant_id",
        "run_id",
        "dispatch_id",
        "dispatch_attempt_id",
        "attempt_id",
        "receive_count",
        "source_arn",
        "trace_id",
        "span_id",
        "operation_id",
        "incident_id",
    },
    "realtime_changefeed": {
        "event",
        "status",
        "tenant_id",
        "run_id",
        "message_id",
        "trace_id",
        "span_id",
    },
}
BROWSER_EVIDENCE_FIELDS = {
    "operation_id",
    "observed",
    "persisted",
    "signature",
    "capture_errors",
}
BROWSER_OBSERVED_FIELDS = {"id", "operation_type", "status"}
BROWSER_PERSISTED_FIELDS = {
    "id",
    "operation_type",
    "status",
    "invalidated_memory_ids",
    "restored_memory_ids",
    "events",
    "effects",
}
BROWSER_EVENT_FIELDS = {"sequence", "status"}
BROWSER_EFFECT_FIELDS = {
    "sequence",
    "effect_type",
    "source_memory_id",
    "result_memory_id",
    "namespace",
}
BROWSER_SIGNATURE_FIELDS = {
    "namespace",
    "operation_id",
    "invalidated_memory_ids",
    "bad",
    "corrected",
}
BROWSER_SIGNATURE_RUN_FIELDS = {
    "run_id",
    "decision_id",
    "status",
    "reflected_memory_id",
    "selection_fingerprint",
    "recommendation_id",
    "approval_approved",
    "execution_status",
    "read_memory_ids",
    "read_count",
    "downstream_lineage_edge_count",
}
BROWSER_CAPTURE_ERROR_FIELDS = {"stage", "type"}
BROWSER_CAPTURE_ERROR_STAGES = {"screenshot", "console", "operations", "database"}


def _load_object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def validate_browser_evidence(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("browser operation evidence must be a JSON object")
    _validate_browser_evidence_projection(value)
    signature = value.get("signature")
    corrected = signature.get("corrected") if isinstance(signature, dict) else None
    if not isinstance(corrected, dict):
        raise ValueError("browser operation evidence is missing the corrected signature")
    run_id = str(corrected.get("run_id") or "")
    if not run_id or len(run_id) > 256 or SECRET_VALUE.search(run_id):
        raise ValueError("browser operation evidence has an invalid corrected run ID")
    if corrected.get("status") != "completed":
        raise ValueError("browser operation evidence did not complete the corrected run")
    return run_id, hashlib.sha256(raw).hexdigest()


def _validate_browser_evidence_projection(value: dict[str, Any]) -> None:
    _reject_unknown_browser_fields(value, BROWSER_EVIDENCE_FIELDS, "operation evidence")
    _validate_browser_string_field(
        value, "operation_id", "operation evidence", nullable=True
    )
    for item in _browser_object_list(value.get("observed", []), "observed operation"):
        _reject_unknown_browser_fields(item, BROWSER_OBSERVED_FIELDS, "observed operation")
        for field in BROWSER_OBSERVED_FIELDS:
            _validate_browser_string_field(item, field, "observed operation")

    persisted = value.get("persisted")
    if persisted is not None:
        if not isinstance(persisted, dict):
            raise ValueError("persisted browser operation must be an object")
        _reject_unknown_browser_fields(
            persisted,
            BROWSER_PERSISTED_FIELDS,
            "persisted browser operation",
        )
        for field in ("id", "operation_type", "status"):
            _validate_browser_string_field(
                persisted, field, "persisted browser operation"
            )
        for field in ("invalidated_memory_ids", "restored_memory_ids"):
            _validate_browser_string_list(
                persisted, field, "persisted browser operation"
            )
        for event in _browser_object_list(
            persisted.get("events", []), "persisted operation event"
        ):
            _reject_unknown_browser_fields(
                event,
                BROWSER_EVENT_FIELDS,
                "persisted operation event",
            )
            _validate_browser_integer_field(
                event, "sequence", "persisted operation event"
            )
            _validate_browser_string_field(
                event, "status", "persisted operation event"
            )
        for effect in _browser_object_list(
            persisted.get("effects", []), "persisted operation effect"
        ):
            _reject_unknown_browser_fields(
                effect,
                BROWSER_EFFECT_FIELDS,
                "persisted operation effect",
            )
            _validate_browser_integer_field(
                effect, "sequence", "persisted operation effect"
            )
            _validate_browser_string_field(
                effect, "effect_type", "persisted operation effect"
            )
            for field in ("source_memory_id", "result_memory_id", "namespace"):
                _validate_browser_string_field(
                    effect,
                    field,
                    "persisted operation effect",
                    nullable=True,
                )

    signature = value.get("signature")
    if signature is not None:
        if not isinstance(signature, dict):
            raise ValueError("browser signature must be an object")
        _reject_unknown_browser_fields(
            signature,
            BROWSER_SIGNATURE_FIELDS,
            "browser signature",
        )
        for field in ("namespace", "operation_id"):
            _validate_browser_string_field(signature, field, "browser signature")
        _validate_browser_string_list(
            signature, "invalidated_memory_ids", "browser signature"
        )
        for label in ("bad", "corrected"):
            run = signature.get(label)
            if run is None:
                continue
            if not isinstance(run, dict):
                raise ValueError(f"browser signature {label} run must be an object")
            _reject_unknown_browser_fields(
                run,
                BROWSER_SIGNATURE_RUN_FIELDS,
                f"browser signature {label} run",
            )
            for field in (
                "run_id",
                "decision_id",
                "status",
                "selection_fingerprint",
                "recommendation_id",
                "execution_status",
            ):
                _validate_browser_string_field(
                    run, field, f"browser signature {label} run"
                )
            _validate_browser_string_field(
                run,
                "reflected_memory_id",
                f"browser signature {label} run",
                nullable=True,
            )
            if "approval_approved" in run and type(run["approval_approved"]) is not bool:
                raise ValueError(
                    f"browser signature {label} run approval_approved must be a boolean"
                )
            _validate_browser_string_list(
                run, "read_memory_ids", f"browser signature {label} run"
            )
            for field in ("read_count", "downstream_lineage_edge_count"):
                _validate_browser_integer_field(
                    run, field, f"browser signature {label} run"
                )

    for error in _browser_object_list(
        value.get("capture_errors", []), "browser capture error"
    ):
        _reject_unknown_browser_fields(
            error,
            BROWSER_CAPTURE_ERROR_FIELDS,
            "browser capture error",
        )
        if (
            error.get("stage") not in BROWSER_CAPTURE_ERROR_STAGES
            or error.get("type") != "capture_failed"
        ):
            raise ValueError("browser capture error is not a stable code")


def _browser_object_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{label} collection must contain only objects")
    return value


def _reject_unknown_browser_fields(
    value: dict[str, Any], allowed: set[str], label: str
) -> None:
    if set(value) - allowed:
        raise ValueError(f"{label} contains unexpected fields")


def _validate_browser_string_field(
    value: dict[str, Any],
    field: str,
    label: str,
    *,
    nullable: bool = False,
) -> None:
    if field not in value:
        return
    field_value = value[field]
    if nullable and field_value is None:
        return
    if not _is_safe_browser_scalar(field_value):
        raise ValueError(f"{label} {field} must be a bounded safe scalar")


def _validate_browser_string_list(
    value: dict[str, Any], field: str, label: str
) -> None:
    if field not in value:
        return
    field_value = value[field]
    if not isinstance(field_value, list) or any(
        not _is_safe_browser_scalar(item) for item in field_value
    ):
        raise ValueError(f"{label} {field} must contain only bounded safe scalars")


def _validate_browser_integer_field(
    value: dict[str, Any], field: str, label: str
) -> None:
    if field in value and type(value[field]) is not int:
        raise ValueError(f"{label} {field} must be an integer")


def _is_safe_browser_scalar(value: Any) -> bool:
    return (
        isinstance(value, str)
        and BROWSER_SAFE_SCALAR.fullmatch(value) is not None
        and SECRET_VALUE.search(value) is None
    )


def validate_provenance(
    run: dict[str, Any],
    provenance: dict[str, Any],
    *,
    repository: str,
    source_revision: str,
    acceptance_run_id: str,
    acceptance_run_attempt: str,
    deployment_environment: str,
) -> tuple[datetime, datetime]:
    if FULL_SHA.fullmatch(source_revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    if INTEGER.fullmatch(acceptance_run_id) is None or INTEGER.fullmatch(
        acceptance_run_attempt
    ) is None:
        raise ValueError("acceptance run identity must use positive integers")
    owner = repository.split("/", 1)[0]
    checks = {
        "repository": (str((run.get("repository") or {}).get("full_name") or ""), repository),
        "run id": (str(run.get("id") or ""), acceptance_run_id),
        "run attempt": (str(run.get("run_attempt") or ""), acceptance_run_attempt),
        "head SHA": (str(run.get("head_sha") or ""), source_revision),
        "event": (str(run.get("event") or ""), "workflow_dispatch"),
        "branch": (str(run.get("head_branch") or ""), "main"),
        "workflow path": (str(run.get("path") or ""), ".github/workflows/live-acceptance.yml"),
        "conclusion": (str(run.get("conclusion") or ""), "success"),
        "actor": (str((run.get("actor") or {}).get("login") or ""), owner),
        "triggering actor": (
            str((run.get("triggering_actor") or {}).get("login") or ""),
            owner,
        ),
        "provenance repository": (str(provenance.get("repository") or ""), repository),
        "provenance run id": (str(provenance.get("run_id") or ""), acceptance_run_id),
        "provenance run attempt": (
            str(provenance.get("run_attempt") or ""),
            acceptance_run_attempt,
        ),
        "provenance SHA": (str(provenance.get("head_sha") or ""), source_revision),
        "acceptance mode": (str(provenance.get("acceptance_mode") or ""), "full"),
        "deployment environment": (
            str(provenance.get("deployment_environment") or ""),
            deployment_environment,
        ),
    }
    for label, (actual, expected) in checks.items():
        if actual != expected:
            raise ValueError(f"{label} does not match the requested full acceptance run")
    if provenance.get("bounded_observability_enabled") is not True:
        raise ValueError("full acceptance did not enable bounded observability")
    started = _timestamp(run.get("run_started_at") or run.get("created_at"), "run start")
    completed = _timestamp(run.get("updated_at"), "run completion")
    if completed <= started:
        raise ValueError("acceptance run completion must follow its start")
    start = started - timedelta(minutes=5)
    end = completed + timedelta(minutes=5)
    if (end - start).total_seconds() > MAX_WINDOW_SECONDS:
        raise ValueError("acceptance log scan window exceeds the four-hour bound")
    return start, end


def _timestamp(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def expected_log_groups(stage: str) -> list[str]:
    if stage not in {"demo", "demo-candidate"}:
        raise ValueError("stage is not allow-listed")
    return [
        f"/aws/lambda/hindsight-{stage}-api",
        f"/aws/lambda/hindsight-{stage}-worker",
        f"/aws/lambda/hindsight-{stage}-changefeed",
    ]


def create_session(*, profile: str | None, region: str) -> Any:
    return boto3.Session(profile_name=profile or None, region_name=region)


def verify_identity(session: Any, *, expected_account_id: str, region: str) -> dict[str, str]:
    if ACCOUNT_ID.fullmatch(expected_account_id) is None:
        raise ValueError("expected account ID must contain twelve digits")
    identity = session.client("sts", region_name=region, config=CLIENT_CONFIG).get_caller_identity()
    if str(identity.get("Account") or "") != expected_account_id:
        raise RuntimeError("authenticated AWS account does not match the protected environment")
    arn = str(identity.get("Arn") or "")
    if not arn:
        raise RuntimeError("AWS caller identity did not include an ARN")
    return {"account_id": expected_account_id, "caller_arn": arn, "region": region}


def collect_logs(
    client: Any,
    *,
    log_groups: list[str],
    start: datetime,
    end: datetime,
    sleep: Any = time.sleep,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    response = client.start_query(
        logGroupNames=log_groups,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()),
        queryString=(
            "fields @timestamp, @log, @message "
            '| filter @message like /"event":"(api_request|run_dispatch|realtime_changefeed)"/ '
            'or (@message like /"event":"worker_record"/ '
            'and @message like /"status":"completed"/) '
            "| sort @timestamp asc "
            f"| limit {MAX_LOG_EVENTS}"
        ),
        limit=MAX_LOG_EVENTS,
    )
    query_id = str(response.get("queryId") or "")
    if not query_id:
        raise RuntimeError("CloudWatch Logs did not acknowledge the bounded query")
    result: dict[str, Any] | None = None
    for _ in range(QUERY_POLLS):
        candidate = client.get_query_results(queryId=query_id)
        status = str(candidate.get("status") or "")
        if status == "Complete":
            result = candidate
            break
        if status in {"Cancelled", "Failed", "Timeout", "Unknown"}:
            raise RuntimeError(f"CloudWatch Logs query ended with status {status}")
        sleep(QUERY_POLL_SECONDS)
    if result is None:
        client.stop_query(queryId=query_id)
        raise RuntimeError("CloudWatch Logs query exceeded the bounded poll budget")
    statistics = result.get("statistics") or {}
    bytes_scanned = float(statistics.get("bytesScanned") or 0)
    if bytes_scanned > MAX_SCAN_BYTES:
        raise RuntimeError("CloudWatch Logs query exceeded the 256 MiB scan bound")
    observations = []
    for row in result.get("results") or []:
        fields = {str(item.get("field")): str(item.get("value") or "") for item in row}
        log_group = fields.get("@log", "").split(":", 1)[-1]
        if log_group not in log_groups:
            raise RuntimeError("CloudWatch Logs returned an unexpected log group")
        message = _extract_structured_event(fields.get("@message", ""))
        event = _validate_log_event(message)
        observations.append(
            {
                "timestamp": fields.get("@timestamp", ""),
                "log_group": log_group,
                **event,
            }
        )
    return observations, {
        "query_id": query_id,
        "bytes_scanned": bytes_scanned,
        "records_scanned": float(statistics.get("recordsScanned") or 0),
        "records_matched": float(statistics.get("recordsMatched") or 0),
    }


def _extract_structured_event(raw_message: str) -> dict[str, Any]:
    messages = [raw_message.strip()]
    try:
        outer = json.loads(messages[0])
    except json.JSONDecodeError:
        outer = None
    if isinstance(outer, dict) and isinstance(outer.get("message"), str):
        messages.insert(0, outer["message"].strip())
    for message in messages:
        for index, character in enumerate(message):
            if character != "{":
                continue
            candidate = message[index:]
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            compact = json.dumps(value, sort_keys=True, separators=(",", ":"))
            if candidate == compact:
                return value
    raise RuntimeError("Lambda log message does not end with a compact structured event")


def _validate_log_event(message: dict[str, Any]) -> dict[str, str]:
    event_name = str(message.get("event") or "")
    allowed = EVENT_FIELDS.get(event_name)
    if allowed is None or set(message) - allowed:
        raise RuntimeError("structured log contains an unexpected field")
    result: dict[str, str] = {}
    for key, raw_value in message.items():
        value = str(raw_value)
        if SECRET_KEY.search(key) or SECRET_VALUE.search(value):
            raise RuntimeError("structured log contains secret-bearing material")
        if len(value) > 256:
            raise RuntimeError("structured log field exceeds the evidence bound")
        result[key] = value
    for required in ("event", "trace_id", "span_id"):
        if not result.get(required):
            raise RuntimeError("structured log is missing active trace correlation")
    return result


def collect_traces(
    client: Any,
    *,
    trace_ids: list[str],
) -> dict[str, dict[str, Any]]:
    if not trace_ids or len(trace_ids) > MAX_CANDIDATE_TRACES:
        raise ValueError("candidate trace count must stay within the bounded batch limit")
    traces: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(trace_ids), TRACE_BATCH_SIZE):
        batch = [xray_trace_id(value) for value in trace_ids[offset : offset + TRACE_BATCH_SIZE]]
        response = client.batch_get_traces(TraceIds=batch)
        for trace in response.get("Traces") or []:
            xray_id = str(trace.get("Id") or "")
            normalized = normalize_trace_id(xray_id)
            if not normalized:
                continue
            nodes: list[dict[str, Any]] = []
            for segment in trace.get("Segments") or []:
                document = json.loads(segment.get("Document") or "{}")
                _trace_nodes(document, nodes)
            traces[normalized] = {
                "xray_trace_id": xray_id,
                "duration_seconds": float(trace.get("Duration") or 0),
                "nodes": nodes,
            }
    return traces


def normalize_trace_id(value: str) -> str:
    match = re.fullmatch(r"1-([0-9a-f]{8})-([0-9a-f]{24})", value)
    if match:
        return "".join(match.groups())
    return value if re.fullmatch(r"[0-9a-f]{32}", value) else ""


def xray_trace_id(value: str) -> str:
    normalized = normalize_trace_id(value)
    if not normalized:
        raise ValueError("candidate trace ID is not a W3C or X-Ray trace ID")
    return f"1-{normalized[:8]}-{normalized[8:]}"


def _trace_nodes(document: Any, nodes: list[dict[str, Any]]) -> None:
    if not isinstance(document, dict):
        return
    node = {
        key: document[key]
        for key in ("name", "id", "parent_id", "origin", "start_time", "end_time")
        if key in document
    }
    if node:
        nodes.append(node)
    for child in document.get("subsegments") or []:
        _trace_nodes(child, nodes)


def correlate(
    logs: list[dict[str, str]],
    traces: dict[str, dict[str, Any]],
    *,
    product_run_id: str,
) -> dict[str, Any]:
    workers = [
        event
        for event in logs
        if event.get("event") == "worker_record"
        and event.get("status") == "completed"
        and event.get("run_id") == product_run_id
        and all(
            event.get(key)
            for key in ("tenant_id", "run_id", "dispatch_id", "dispatch_attempt_id", "trace_id")
        )
    ]
    for worker in workers:
        trace_id = worker["trace_id"]
        dispatch = next(
            (
                event
                for event in logs
                if event.get("event") == "run_dispatch"
                and event.get("status") == "sent"
                and event.get("trace_id") == trace_id
                and event.get("tenant_id") == worker["tenant_id"]
                and event.get("run_id") == worker["run_id"]
                and event.get("dispatch_id") == worker["dispatch_id"]
                and event.get("dispatch_attempt_id") == worker["dispatch_attempt_id"]
                and event.get("message_id") == worker.get("message_id")
            ),
            None,
        )
        api = next(
            (
                event
                for event in logs
                if event.get("event") == "api_request"
                and event.get("trace_id") == trace_id
                and event.get("tenant_id") == worker["tenant_id"]
                and event.get("status") == "202"
            ),
            None,
        )
        trace = traces.get(trace_id)
        if dispatch is None or api is None or trace is None:
            continue
        names = {str(node.get("name") or "") for node in trace["nodes"]}
        if not {"hindsight.api.request", "hindsight.worker.message"}.issubset(names):
            continue
        realtime = next(
            (
                event
                for event in logs
                if event.get("event") == "realtime_changefeed"
                and event.get("status") == "delivered"
                and event.get("tenant_id") == worker["tenant_id"]
                and event.get("run_id") == worker["run_id"]
            ),
            None,
        )
        if realtime is None:
            continue
        return {
            "tenant_id": worker["tenant_id"],
            "run_id": worker["run_id"],
            "trace_id": trace_id,
            "dispatch_id": worker["dispatch_id"],
            "dispatch_attempt_id": worker["dispatch_attempt_id"],
            "api": api,
            "dispatch": dispatch,
            "worker": worker,
            "realtime": realtime,
            "trace": trace,
        }
    raise RuntimeError("no complete API-to-queue-to-worker and realtime correlation was found")


def candidate_trace_ids(logs: list[dict[str, str]], *, product_run_id: str) -> list[str]:
    candidates: list[str] = []
    for worker in logs:
        if worker.get("event") != "worker_record" or worker.get("status") != "completed":
            continue
        if worker.get("run_id") != product_run_id:
            continue
        if not all(
            worker.get(key)
            for key in (
                "tenant_id",
                "run_id",
                "dispatch_id",
                "dispatch_attempt_id",
                "message_id",
            )
        ):
            continue
        trace_id = normalize_trace_id(worker.get("trace_id", ""))
        if not trace_id:
            continue
        matching_dispatch = any(
            event.get("event") == "run_dispatch"
            and event.get("status") == "sent"
            and event.get("trace_id") == trace_id
            and event.get("tenant_id") == worker.get("tenant_id")
            and event.get("run_id") == worker.get("run_id")
            and event.get("dispatch_id") == worker.get("dispatch_id")
            and event.get("dispatch_attempt_id") == worker.get("dispatch_attempt_id")
            and event.get("message_id") == worker.get("message_id")
            for event in logs
        )
        matching_api = any(
            event.get("event") == "api_request"
            and event.get("status") == "202"
            and event.get("trace_id") == trace_id
            and event.get("tenant_id") == worker.get("tenant_id")
            for event in logs
        )
        matching_realtime = any(
            event.get("event") == "realtime_changefeed"
            and event.get("status") == "delivered"
            and event.get("tenant_id") == worker.get("tenant_id")
            and event.get("run_id") == worker.get("run_id")
            for event in logs
        )
        if matching_dispatch and matching_api and matching_realtime and trace_id not in candidates:
            candidates.append(trace_id)
        if len(candidates) == MAX_CANDIDATE_TRACES:
            break
    if not candidates:
        raise RuntimeError("bounded logs did not contain a complete correlation candidate")
    return candidates


def collect_correlation_evidence(
    logs_client: Any,
    xray_client: Any,
    *,
    log_groups: list[str],
    start: datetime,
    end: datetime,
    product_run_id: str,
    sleep: Any = time.sleep,
) -> tuple[dict[str, Any], dict[str, Any], int, int, int]:
    logs, log_statistics = collect_logs(
        logs_client,
        log_groups=log_groups,
        start=start,
        end=end,
        sleep=sleep,
    )
    candidate_ids = candidate_trace_ids(logs, product_run_id=product_run_id)
    last_error: Exception | None = None
    for attempt in range(1, COLLECTION_ATTEMPTS + 1):
        try:
            traces = collect_traces(xray_client, trace_ids=candidate_ids)
            correlation = correlate(logs, traces, product_run_id=product_run_id)
            return (
                correlation,
                log_statistics,
                len(candidate_ids),
                len(traces),
                attempt,
            )
        except (BotoCoreError, ClientError, RuntimeError) as exc:
            last_error = exc
            if attempt < COLLECTION_ATTEMPTS:
                sleep(COLLECTION_DELAY_SECONDS)
    raise RuntimeError("bounded trace correlation attempts were exhausted") from last_error


def build_report(
    *,
    source_revision: str,
    repository: str,
    acceptance_run_id: str,
    acceptance_run_attempt: str,
    product_run_id: str,
    browser_evidence_sha256: str,
    deployment_environment: str,
    identity: dict[str, str],
    start: datetime,
    end: datetime,
    log_groups: list[str],
    correlation: dict[str, Any],
    log_statistics: dict[str, Any],
    trace_ids_requested: int,
    traces_returned: int,
    trace_collection_attempt: int,
    alert: dict[str, Any],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema_version": 1,
        "kind": "hosted_observability_evidence",
        "source_revision": source_revision,
        "repository": repository,
        "acceptance": {
            "workflow_path": ".github/workflows/live-acceptance.yml",
            "run_id": acceptance_run_id,
            "run_attempt": acceptance_run_attempt,
            "mode": "full",
            "bounded_observability_enabled": True,
            "product_run_id": product_run_id,
            "browser_operation_sha256": browser_evidence_sha256,
        },
        "environment": {
            "deployment_environment": deployment_environment,
            **identity,
            "log_groups": log_groups,
        },
        "method": {
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "log_query_attempts": 1,
            "trace_collection_attempt": trace_collection_attempt,
            "maximum_trace_collection_attempts": COLLECTION_ATTEMPTS,
            "maximum_log_events": MAX_LOG_EVENTS,
            "maximum_candidate_traces": MAX_CANDIDATE_TRACES,
            "maximum_scan_bytes_per_query": MAX_SCAN_BYTES,
        },
        "raw_observations": {
            "correlation": correlation,
            "log_query": log_statistics,
            "trace_ids_requested": trace_ids_requested,
            "traces_returned": traces_returned,
            "alert": alert,
        },
        "limitations": [
            "The controlled SQS receiver proves machine delivery and deletion, not a separate human response.",
            "The bounded acceptance window and configured sampling rate may omit other valid traces.",
            "Realtime evidence is independently traced and is correlated by tenant and run identity.",
        ],
        "generated_at": datetime.now(UTC).isoformat(),
    }
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    report["evidence_sha256"] = hashlib.sha256(canonical).hexdigest()
    return report


def validate_report_digest(report: dict[str, Any]) -> bool:
    candidate = dict(report)
    digest = str(candidate.pop("evidence_sha256", ""))
    canonical = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
    return bool(re.fullmatch(r"[0-9a-f]{64}", digest)) and hashlib.sha256(canonical).hexdigest() == digest


def _write_report(report: dict[str, Any], output: Path, checksum_output: Path) -> None:
    if not validate_report_digest(report):
        raise RuntimeError("evidence payload digest is invalid")
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered)
    checksum_output.write_text(f"{hashlib.sha256(rendered.encode()).hexdigest()}  {output.name}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--acceptance-run-id", required=True)
    parser.add_argument("--acceptance-run-attempt", required=True)
    parser.add_argument("--acceptance-run", type=Path, required=True)
    parser.add_argument("--acceptance-provenance", type=Path, required=True)
    parser.add_argument("--browser-evidence", type=Path, required=True)
    parser.add_argument("--deployment-environment", choices=("demo", "demo-candidate"), required=True)
    parser.add_argument("--stage", choices=("demo", "demo-candidate"), required=True)
    parser.add_argument("--expected-account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checksum-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        run = _load_object(args.acceptance_run, "acceptance run")
        provenance = _load_object(args.acceptance_provenance, "acceptance provenance")
        product_run_id, browser_evidence_sha256 = validate_browser_evidence(
            args.browser_evidence
        )
        if args.stage != args.deployment_environment:
            raise ValueError("stage must match the protected deployment environment")
        start, end = validate_provenance(
            run,
            provenance,
            repository=args.repository,
            source_revision=args.source_revision,
            acceptance_run_id=args.acceptance_run_id,
            acceptance_run_attempt=args.acceptance_run_attempt,
            deployment_environment=args.deployment_environment,
        )
        groups = expected_log_groups(args.stage)
        session = create_session(profile=args.profile, region=args.region)
        identity = verify_identity(
            session,
            expected_account_id=args.expected_account_id,
            region=args.region,
        )
        logs_client = session.client("logs", region_name=args.region, config=CLIENT_CONFIG)
        xray_client = session.client("xray", region_name=args.region, config=CLIENT_CONFIG)
        (
            correlation,
            log_statistics,
            trace_ids_requested,
            traces_returned,
            attempt,
        ) = collect_correlation_evidence(
            logs_client,
            xray_client,
            log_groups=groups,
            start=start,
            end=end,
            product_run_id=product_run_id,
        )
        operational_topic_arn = (
            f"arn:aws:sns:{args.region}:{args.expected_account_id}:hindsight-{args.stage}-alerts"
        )
        budget_topic_arn = (
            f"arn:aws:sns:us-east-1:{args.expected_account_id}:hindsight-{args.stage}-budget-alerts"
        )
        alert = exercise(
            alarm_name=f"hindsight-{args.stage}-exact-release-probe",
            receiver_queue_name=f"hindsight-{args.stage}-alert-receiver",
            operational_topic_arn=operational_topic_arn,
            budget_topic_arn=budget_topic_arn,
            expected_account_id=args.expected_account_id,
            region=args.region,
            stage=args.stage,
            profile=args.profile,
            source_revision=args.source_revision,
            session=session,
        )
        report = build_report(
            source_revision=args.source_revision,
            repository=args.repository,
            acceptance_run_id=args.acceptance_run_id,
            acceptance_run_attempt=args.acceptance_run_attempt,
            product_run_id=product_run_id,
            browser_evidence_sha256=browser_evidence_sha256,
            deployment_environment=args.deployment_environment,
            identity=identity,
            start=start,
            end=end,
            log_groups=groups,
            correlation=correlation,
            log_statistics=log_statistics,
            trace_ids_requested=trace_ids_requested,
            traces_returned=traces_returned,
            trace_collection_attempt=attempt,
            alert=alert,
        )
        _write_report(report, args.output, args.checksum_output)
        return 0
    except (BotoCoreError, ClientError, OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"observability evidence failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
