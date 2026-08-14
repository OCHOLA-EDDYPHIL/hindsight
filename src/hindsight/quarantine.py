"""Strict DynamoDB ledger for terminal worker messages."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import boto3
from boto3.dynamodb.conditions import Attr, Key

from hindsight.aws import aws_client_config

QUARANTINE_TABLE_ENV = "HINDSIGHT_QUARANTINE_TABLE"
QUARANTINE_INDEX_ENV = "HINDSIGHT_QUARANTINE_INDEX"
QUARANTINE_INDEX_DEFAULT = "quarantine-status-created-at-index"
QUARANTINE_METRIC_NAMESPACE_ENV = "HINDSIGHT_QUARANTINE_METRIC_NAMESPACE"
QUARANTINE_METRIC_NAMESPACE_DEFAULT = "Hindsight/Quarantine"
QUARANTINE_METRIC_STAGE_ENV = "HINDSIGHT_QUARANTINE_METRIC_STAGE"
QUARANTINE_SCHEMA_VERSION = 1
QUARANTINE_STATUSES = frozenset({"quarantined", "redrive_pending", "redriven"})
QUARANTINE_REASONS = frozenset(
    {
        "invalid_envelope",
        "malformed_json",
        "run_attempts_exhausted",
        "run_not_found",
        "unsupported_command",
    }
)
QUARANTINE_WORK_KINDS = frozenset({"run", "operation", "unknown"})
QUARANTINE_COMMANDS = frozenset(
    {
        "consolidation",
        "memory_operation",
        "resume",
        "start",
        "unsupported",
    }
)
_MESSAGE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_STAGE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CORE_KEYS = frozenset(
    {
        "schema_version",
        "quarantine_id",
        "source_arn",
        "source_message_id",
        "raw_body_sha256",
        "reason_code",
        "work_kind",
        "command",
        "receive_count",
        "tenant_id",
        "run_id",
        "operation_id",
        "command_generation",
    }
)
_STORED_KEYS = _CORE_KEYS | frozenset(
    {
        "status",
        "created_at",
        "record_sha256",
        "redrive_effect_id",
        "redrive_binding_sha256",
        "redrive_started_at",
        "redriven_at",
        "redriven_run_id",
    }
)
_IDENTITY_KEYS = _CORE_KEYS - {"receive_count"}
_STORED_INTEGER_RANGES = {
    "schema_version": (QUARANTINE_SCHEMA_VERSION, QUARANTINE_SCHEMA_VERSION),
    "receive_count": (1, 2**63 - 1),
    "command_generation": (0, 2**63 - 1),
}


class QuarantineError(RuntimeError):
    """Base class for quarantine ledger failures."""


class QuarantineConflictError(QuarantineError):
    """Raised when a stable quarantine ID is bound to different content."""


class QuarantineRecordError(QuarantineError):
    """Raised when a quarantine record is outside the strict schema."""


@dataclass(frozen=True)
class QuarantineWrite:
    item: dict[str, Any]
    created: bool


def stable_quarantine_id(*, source_arn: str, source_message_id: str) -> str:
    """Derive the stable ledger key from the trusted SQS delivery identity."""

    normalized_arn = _source_arn(source_arn)
    normalized_message_id = _message_id(source_message_id)
    canonical = _canonical_bytes(
        {"source_arn": normalized_arn, "source_message_id": normalized_message_id}
    )
    return f"q_{hashlib.sha256(canonical).hexdigest()}"


def raw_body_digest(raw_body: str) -> str:
    """Return the SHA-256 of the exact UTF-8 SQS body without retaining it."""

    if not isinstance(raw_body, str):
        raise QuarantineRecordError("raw body must be a string")
    return hashlib.sha256(raw_body.encode("utf-8")).hexdigest()


def persist_quarantine_record(
    *,
    table: Any,
    source_arn: str,
    source_message_id: str,
    raw_body: str,
    reason_code: str,
    work_kind: str,
    command: str,
    receive_count: int | None = None,
    tenant_id: str | None = None,
    run_id: str | None = None,
    operation_id: str | None = None,
    command_generation: int | None = None,
    now: datetime | None = None,
) -> QuarantineWrite:
    """Conditionally create one allowlisted terminal record."""

    core: dict[str, Any] = {
        "schema_version": QUARANTINE_SCHEMA_VERSION,
        "quarantine_id": stable_quarantine_id(
            source_arn=source_arn,
            source_message_id=source_message_id,
        ),
        "source_arn": _source_arn(source_arn),
        "source_message_id": _message_id(source_message_id),
        "raw_body_sha256": raw_body_digest(raw_body),
        "reason_code": _member(reason_code, QUARANTINE_REASONS, "reason code"),
        "work_kind": _member(work_kind, QUARANTINE_WORK_KINDS, "work kind"),
        "command": _member(command, QUARANTINE_COMMANDS, "command"),
    }
    if receive_count is not None:
        if type(receive_count) is not int or not 1 <= receive_count <= 2**63 - 1:
            raise QuarantineRecordError("receive count must be a positive integer")
        core["receive_count"] = receive_count
    if tenant_id is not None:
        core["tenant_id"] = _uuid(tenant_id, "tenant ID")
    if run_id is not None:
        core["run_id"] = _uuid(run_id, "run ID")
    if operation_id is not None:
        core["operation_id"] = _uuid(operation_id, "operation ID")
    if command_generation is not None:
        if type(command_generation) is not int or not 0 <= command_generation <= 2**63 - 1:
            raise QuarantineRecordError("command generation must be a non-negative integer")
        core["command_generation"] = command_generation
    _validate_core_relationships(core)
    record_sha256 = hashlib.sha256(_canonical_bytes(core)).hexdigest()
    item = {
        **core,
        "status": "quarantined",
        "created_at": _timestamp(now or datetime.now(UTC)),
        "record_sha256": record_sha256,
    }
    try:
        table.put_item(
            Item=item,
            ConditionExpression=Attr("quarantine_id").not_exists(),
        )
        return QuarantineWrite(item=dict(item), created=True)
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        existing = table.get_item(
            Key={"quarantine_id": core["quarantine_id"]},
            ConsistentRead=True,
        ).get("Item")
        validated = validate_stored_quarantine_item(existing)
        if _stored_identity(validated) != _identity(core):
            raise QuarantineConflictError(
                "quarantine ID is already bound to different terminal work"
            ) from None
        return QuarantineWrite(item=validated, created=False)


def validate_stored_quarantine_item(item: Any) -> dict[str, Any]:
    """Fail closed if a stored ledger row has unknown or inconsistent fields."""

    if not isinstance(item, dict) or not item:
        raise QuarantineRecordError("quarantine record does not exist")
    normalized_item = _normalize_stored_integers(item)
    unknown = set(normalized_item) - _STORED_KEYS
    if unknown:
        raise QuarantineRecordError("quarantine record contains unknown fields")
    core = _stored_core(normalized_item)
    if (
        type(core.get("schema_version")) is not int
        or core["schema_version"] != QUARANTINE_SCHEMA_VERSION
    ):
        raise QuarantineRecordError("quarantine schema version is unsupported")
    expected_id = stable_quarantine_id(
        source_arn=str(core.get("source_arn") or ""),
        source_message_id=str(core.get("source_message_id") or ""),
    )
    if core.get("quarantine_id") != expected_id:
        raise QuarantineRecordError("quarantine identity does not match its source")
    digest = str(core.get("raw_body_sha256") or "")
    if _DIGEST.fullmatch(digest) is None:
        raise QuarantineRecordError("quarantine body digest is invalid")
    _member(str(core.get("reason_code") or ""), QUARANTINE_REASONS, "reason code")
    _member(str(core.get("work_kind") or ""), QUARANTINE_WORK_KINDS, "work kind")
    _member(str(core.get("command") or ""), QUARANTINE_COMMANDS, "command")
    if "receive_count" in core and (
        type(core["receive_count"]) is not int or not 1 <= core["receive_count"] <= 2**63 - 1
    ):
        raise QuarantineRecordError("quarantine receive count is invalid")
    for key, label in (
        ("tenant_id", "tenant ID"),
        ("run_id", "run ID"),
        ("operation_id", "operation ID"),
    ):
        if key in core:
            _uuid(core[key], label)
    generation = core.get("command_generation")
    if generation is not None and (type(generation) is not int or not 0 <= generation <= 2**63 - 1):
        raise QuarantineRecordError("quarantine command generation is invalid")
    _validate_core_relationships(core)
    expected_record_digest = hashlib.sha256(_canonical_bytes(core)).hexdigest()
    if normalized_item.get("record_sha256") != expected_record_digest:
        raise QuarantineRecordError("quarantine record digest does not match")
    if normalized_item.get("status") not in QUARANTINE_STATUSES:
        raise QuarantineRecordError("quarantine status is invalid")
    status = str(normalized_item["status"])
    redrive_identity = {
        "redrive_effect_id",
        "redrive_binding_sha256",
        "redrive_started_at",
    }
    redrive_completion = {"redriven_at", "redriven_run_id"}
    if status == "quarantined" and (redrive_identity | redrive_completion) & set(normalized_item):
        raise QuarantineRecordError("quarantined record has redrive fields")
    if status == "redrive_pending" and (
        not redrive_identity <= set(normalized_item) or redrive_completion & set(normalized_item)
    ):
        raise QuarantineRecordError("pending redrive fields are incomplete")
    if status == "redriven" and not (redrive_identity | redrive_completion) <= set(normalized_item):
        raise QuarantineRecordError("completed redrive fields are incomplete")
    _parse_timestamp(normalized_item.get("created_at"))
    for key in ("redrive_binding_sha256",):
        if key in normalized_item and _DIGEST.fullmatch(str(normalized_item[key])) is None:
            raise QuarantineRecordError("quarantine redrive digest is invalid")
    for key in ("redrive_effect_id", "redriven_run_id"):
        if key in normalized_item:
            _uuid(normalized_item[key], key.replace("_", " "))
    for key in ("redrive_started_at", "redriven_at"):
        if key in normalized_item:
            _parse_timestamp(normalized_item[key])
    return normalized_item


def quarantine_table_from_env(*, session: Any | None = None) -> Any:
    """Create the configured DynamoDB table resource."""

    table_name = str(os.environ.get(QUARANTINE_TABLE_ENV) or "").strip()
    if not table_name:
        raise QuarantineError(f"{QUARANTINE_TABLE_ENV} is required")
    dynamodb = (session or boto3.Session()).resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        config=aws_client_config(read_timeout=10),
    )
    return dynamodb.Table(table_name)


def report_quarantine_metrics(
    *,
    table: Any,
    cloudwatch_client: Any,
    stage: str,
    index_name: str = QUARANTINE_INDEX_DEFAULT,
    namespace: str = QUARANTINE_METRIC_NAMESPACE_DEFAULT,
    now: datetime | None = None,
) -> dict[str, int]:
    """Publish count and oldest-age gauges for terminal and pending records."""

    normalized_stage = str(stage or "").strip()
    if _STAGE.fullmatch(normalized_stage) is None:
        raise QuarantineRecordError("quarantine metric stage is invalid")
    if not index_name or len(index_name) > 255:
        raise QuarantineRecordError("quarantine index name is invalid")
    if not namespace or len(namespace) > 255:
        raise QuarantineRecordError("quarantine metric namespace is invalid")
    current = now or datetime.now(UTC)
    count = 0
    oldest: datetime | None = None
    for status in ("quarantined", "redrive_pending"):
        start_key = None
        while True:
            kwargs: dict[str, Any] = {
                "IndexName": index_name,
                "KeyConditionExpression": Key("status").eq(status),
                "ProjectionExpression": "quarantine_id, created_at",
            }
            if start_key is not None:
                kwargs["ExclusiveStartKey"] = start_key
            response = table.query(**kwargs)
            items = response.get("Items") or []
            count += len(items)
            for item in items:
                created_at = _parse_timestamp(item.get("created_at"))
                if oldest is None or created_at < oldest:
                    oldest = created_at
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
    oldest_age = max(0, int((current - oldest).total_seconds())) if oldest is not None else 0
    cloudwatch_client.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {
                "MetricName": "QuarantineRecordCount",
                "Dimensions": [{"Name": "Stage", "Value": normalized_stage}],
                "Value": count,
                "Unit": "Count",
            },
            {
                "MetricName": "OldestRecordAgeSeconds",
                "Dimensions": [{"Name": "Stage", "Value": normalized_stage}],
                "Value": oldest_age,
                "Unit": "Seconds",
            },
        ],
    )
    return {"count": count, "oldest_age_seconds": oldest_age}


def _stored_core(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in _CORE_KEYS if key in item}


def _normalize_stored_integers(item: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(item)
    for key, (minimum, maximum) in _STORED_INTEGER_RANGES.items():
        value = normalized.get(key)
        if not isinstance(value, Decimal):
            continue
        if (
            not value.is_finite()
            or value != value.to_integral_value()
            or value < minimum
            or value > maximum
        ):
            raise QuarantineRecordError(f"quarantine {key.replace('_', ' ')} is invalid")
        normalized[key] = int(value)
    return normalized


def _stored_identity(item: dict[str, Any]) -> dict[str, Any]:
    return {key: item[key] for key in _IDENTITY_KEYS if key in item}


def _identity(core: dict[str, Any]) -> dict[str, Any]:
    return {key: core[key] for key in _IDENTITY_KEYS if key in core}


def _validate_core_relationships(core: dict[str, Any]) -> None:
    work_kind = core.get("work_kind")
    reason = core.get("reason_code")
    if work_kind == "run":
        required = {"tenant_id", "run_id", "command_generation"}
        if core.get("command") not in {"start", "resume"} or not required <= set(core):
            raise QuarantineRecordError("run quarantine identity is incomplete")
        if reason != "run_attempts_exhausted":
            raise QuarantineRecordError("only exhausted runs may be stored as run work")
    elif any(key in core for key in ("run_id", "command_generation")):
        raise QuarantineRecordError("non-run quarantine cannot retain run identity")
    if work_kind == "operation" and "operation_id" not in core:
        raise QuarantineRecordError("operation quarantine identity is incomplete")
    if work_kind != "operation" and "operation_id" in core:
        raise QuarantineRecordError("non-operation quarantine cannot retain operation identity")


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _source_arn(value: str) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 2048
        or any(character.isspace() for character in normalized)
    ):
        raise QuarantineRecordError("source ARN is invalid")
    if not (normalized.startswith("arn:") or normalized.startswith("local:sqs:")):
        raise QuarantineRecordError("source ARN is invalid")
    return normalized


def _message_id(value: str) -> str:
    normalized = str(value or "").strip()
    if _MESSAGE_ID.fullmatch(normalized) is None:
        raise QuarantineRecordError("source message ID is invalid")
    return normalized


def _member(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        raise QuarantineRecordError(f"quarantine {label} is invalid")
    return value


def _uuid(value: object, label: str) -> str:
    try:
        normalized = str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise QuarantineRecordError(f"quarantine {label} is invalid") from exc
    if str(value) != normalized:
        raise QuarantineRecordError(f"quarantine {label} is not canonical")
    return normalized


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise QuarantineRecordError("quarantine timestamp must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise QuarantineRecordError("quarantine timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise QuarantineRecordError("quarantine timestamp is invalid") from exc
    return parsed.astimezone(UTC)
