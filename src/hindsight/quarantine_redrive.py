"""Owner-confirmed, idempotent redrive for quarantined run work."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from boto3.dynamodb.conditions import Attr

from hindsight.quarantine import validate_stored_quarantine_item
from hindsight.runs import (
    RunConflictError,
    RunIdempotencyConflictError,
    RunNotFoundError,
    get_run,
    redrive_exhausted_run,
)
from hindsight.tenant import tenant_scope


class QuarantineRedriveError(RuntimeError):
    """Raised when a quarantine redrive is not exactly authorized or eligible."""


@dataclass(frozen=True)
class QuarantineRedriveBinding:
    """Deterministic identity shared by redrive execution and recovery."""

    quarantine_id: str
    raw_body_sha256: str
    confirmation: str
    binding_sha256: str
    effect_id: str
    idempotency_key: str


def quarantine_redrive_binding(
    *,
    quarantine_id: str,
    raw_body_sha256: str,
) -> QuarantineRedriveBinding:
    """Return the canonical confirmation and one-effect identity."""

    normalized_id = _quarantine_id(quarantine_id)
    normalized_digest = _digest(raw_body_sha256)
    confirmation = f"redrive:{normalized_id}:{normalized_digest}"
    binding_sha256 = hashlib.sha256(
        f"{normalized_id}:{normalized_digest}".encode("utf-8")
    ).hexdigest()
    effect_id = str(
        uuid5(
            NAMESPACE_URL,
            f"hindsight:quarantine-redrive:{normalized_id}:{normalized_digest}",
        )
    )
    return QuarantineRedriveBinding(
        quarantine_id=normalized_id,
        raw_body_sha256=normalized_digest,
        confirmation=confirmation,
        binding_sha256=binding_sha256,
        effect_id=effect_id,
        idempotency_key=f"quarantine-redrive:{effect_id}",
    )


def redrive_quarantined_run(
    *,
    table: Any,
    quarantine_id: str,
    raw_body_sha256: str,
    confirmation: str,
    repository_owner: str,
    actor: str,
    triggering_actor: str,
    db_url: str,
    expected_record_sha256: str | None = None,
    dispatch_available_at: datetime | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one fresh run from an exhausted source under an exact owner gate."""

    _require_owner_gate(
        repository_owner=repository_owner,
        actor=actor,
        triggering_actor=triggering_actor,
    )
    if dispatch_available_at is not None and (
        not isinstance(dispatch_available_at, datetime)
        or dispatch_available_at.tzinfo is None
        or dispatch_available_at.utcoffset() is None
    ):
        raise QuarantineRedriveError("dispatch_available_at must include a timezone")
    binding = quarantine_redrive_binding(
        quarantine_id=quarantine_id,
        raw_body_sha256=raw_body_sha256,
    )
    if not hmac.compare_digest(confirmation, binding.confirmation):
        raise QuarantineRedriveError("redrive confirmation phrase does not match")
    expected_record_digest = (
        _digest(expected_record_sha256) if expected_record_sha256 is not None else None
    )

    item = _get_item(table=table, quarantine_id=binding.quarantine_id)
    record_sha256 = str(item["record_sha256"])
    _require_record_identity(
        item=item,
        binding=binding,
        record_sha256=record_sha256,
        error="quarantine record identity does not match",
    )
    if expected_record_digest is not None and not hmac.compare_digest(
        record_sha256,
        expected_record_digest,
    ):
        raise QuarantineRedriveError("quarantine record digest does not match")
    _require_run_item(item)
    item = _claim_redrive(
        table=table,
        item=item,
        effect_id=binding.effect_id,
        binding_sha256=binding.binding_sha256,
        raw_body_sha256=binding.raw_body_sha256,
        record_sha256=record_sha256,
        now=now,
    )
    if item["status"] == "redriven":
        run_id = str(item.get("redriven_run_id") or "")
        if not run_id:
            raise QuarantineRedriveError("completed redrive has no run identity")
        with tenant_scope(str(item["tenant_id"])):
            run = get_run(run_id=run_id, db_url=db_url)
        if run is None:
            raise QuarantineRedriveError("redriven run does not exist")
        return _result(item=item, run=run, created=False)

    with tenant_scope(str(item["tenant_id"])):
        try:
            run, created = redrive_exhausted_run(
                run_id=str(item["run_id"]),
                command=str(item["command"]),
                command_generation=int(item["command_generation"]),
                idempotency_key=binding.idempotency_key,
                dispatch_available_at=dispatch_available_at,
                db_url=db_url,
            )
        except (RunConflictError, RunIdempotencyConflictError, RunNotFoundError, ValueError) as exc:
            raise QuarantineRedriveError("quarantined source run cannot be redriven") from exc
    completed_at = _timestamp(now or datetime.now(UTC))
    try:
        response = table.update_item(
            Key={"quarantine_id": binding.quarantine_id},
            UpdateExpression=(
                "SET #status = :redriven, redriven_at = if_not_exists(redriven_at, :at), "
                "redriven_run_id = if_not_exists(redriven_run_id, :run_id)"
            ),
            ConditionExpression=(
                Attr("status").eq("redrive_pending")
                & Attr("redrive_effect_id").eq(binding.effect_id)
                & Attr("redrive_binding_sha256").eq(binding.binding_sha256)
                & Attr("raw_body_sha256").eq(binding.raw_body_sha256)
                & Attr("record_sha256").eq(record_sha256)
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":redriven": "redriven",
                ":at": completed_at,
                ":run_id": str(run["id"]),
            },
            ReturnValues="ALL_NEW",
        )
        completed = validate_stored_quarantine_item(response.get("Attributes"))
        _require_record_identity(
            item=completed,
            binding=binding,
            record_sha256=record_sha256,
            error="quarantine redrive completion conflicted",
        )
        if (
            completed.get("status") != "redriven"
            or completed.get("redrive_effect_id") != binding.effect_id
            or completed.get("redrive_binding_sha256") != binding.binding_sha256
            or completed.get("redriven_run_id") != str(run["id"])
        ):
            raise QuarantineRedriveError("quarantine redrive completion conflicted")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        completed = _get_item(table=table, quarantine_id=binding.quarantine_id)
        if (
            completed.get("status") != "redriven"
            or completed.get("redrive_effect_id") != binding.effect_id
            or completed.get("redrive_binding_sha256") != binding.binding_sha256
            or completed.get("redriven_run_id") != str(run["id"])
            or not _has_record_identity(
                item=completed,
                binding=binding,
                record_sha256=record_sha256,
            )
        ):
            raise QuarantineRedriveError("quarantine redrive completion conflicted") from None
    return _result(item=completed, run=run, created=created)


def _claim_redrive(
    *,
    table: Any,
    item: dict[str, Any],
    effect_id: str,
    binding_sha256: str,
    raw_body_sha256: str,
    record_sha256: str,
    now: datetime | None,
) -> dict[str, Any]:
    status = item["status"]
    if status in {"redrive_pending", "redriven"}:
        if (
            item.get("redrive_effect_id") != effect_id
            or item.get("redrive_binding_sha256") != binding_sha256
            or item.get("raw_body_sha256") != raw_body_sha256
            or item.get("record_sha256") != record_sha256
        ):
            raise QuarantineRedriveError("quarantine item is bound to another redrive")
        return item
    if status != "quarantined":
        raise QuarantineRedriveError("quarantine item is not redrivable")
    started_at = _timestamp(now or datetime.now(UTC))
    try:
        response = table.update_item(
            Key={"quarantine_id": item["quarantine_id"]},
            UpdateExpression=(
                "SET #status = :pending, redrive_effect_id = :effect_id, "
                "redrive_binding_sha256 = :binding_sha256, "
                "redrive_started_at = :started_at"
            ),
            ConditionExpression=(
                Attr("status").eq("quarantined")
                & Attr("raw_body_sha256").eq(raw_body_sha256)
                & Attr("record_sha256").eq(record_sha256)
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":pending": "redrive_pending",
                ":effect_id": effect_id,
                ":binding_sha256": binding_sha256,
                ":started_at": started_at,
            },
            ReturnValues="ALL_NEW",
        )
        claimed = validate_stored_quarantine_item(response.get("Attributes"))
        if (
            claimed.get("status") != "redrive_pending"
            or claimed.get("redrive_effect_id") != effect_id
            or claimed.get("redrive_binding_sha256") != binding_sha256
            or claimed.get("raw_body_sha256") != raw_body_sha256
            or claimed.get("record_sha256") != record_sha256
        ):
            raise QuarantineRedriveError("quarantine item is bound to another redrive")
        return claimed
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raced = _get_item(table=table, quarantine_id=str(item["quarantine_id"]))
        if (
            raced.get("status") not in {"redrive_pending", "redriven"}
            or raced.get("redrive_effect_id") != effect_id
            or raced.get("redrive_binding_sha256") != binding_sha256
            or raced.get("raw_body_sha256") != raw_body_sha256
            or raced.get("record_sha256") != record_sha256
        ):
            raise QuarantineRedriveError("quarantine item is bound to another redrive") from None
        return raced


def _get_item(*, table: Any, quarantine_id: str) -> dict[str, Any]:
    response = table.get_item(
        Key={"quarantine_id": quarantine_id},
        ConsistentRead=True,
    )
    try:
        return validate_stored_quarantine_item(response.get("Item"))
    except RuntimeError as exc:
        raise QuarantineRedriveError("quarantine record is unavailable or invalid") from exc


def _require_run_item(item: dict[str, Any]) -> None:
    if item.get("work_kind") != "run" or item.get("reason_code") != "run_attempts_exhausted":
        raise QuarantineRedriveError("only exhausted run items may be redriven")


def _has_record_identity(
    *,
    item: dict[str, Any],
    binding: QuarantineRedriveBinding,
    record_sha256: str,
) -> bool:
    return (
        item.get("quarantine_id") == binding.quarantine_id
        and hmac.compare_digest(str(item.get("raw_body_sha256") or ""), binding.raw_body_sha256)
        and hmac.compare_digest(str(item.get("record_sha256") or ""), record_sha256)
    )


def _require_record_identity(
    *,
    item: dict[str, Any],
    binding: QuarantineRedriveBinding,
    record_sha256: str,
    error: str,
) -> None:
    if not _has_record_identity(
        item=item,
        binding=binding,
        record_sha256=record_sha256,
    ):
        raise QuarantineRedriveError(error)


def _require_owner_gate(*, repository_owner: str, actor: str, triggering_actor: str) -> None:
    owner = str(repository_owner or "").strip()
    resolved_actor = str(actor or "").strip()
    resolved_triggering_actor = str(triggering_actor or "").strip()
    if not owner or resolved_actor != owner or resolved_triggering_actor != owner:
        raise QuarantineRedriveError("quarantine redrive requires the repository owner")


def _quarantine_id(value: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) != 66 or not normalized.startswith("q_"):
        raise QuarantineRedriveError("quarantine ID is invalid")
    _digest(normalized[2:])
    return normalized


def _digest(value: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise QuarantineRedriveError("SHA-256 digest is invalid")
    return normalized


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise QuarantineRedriveError("redrive timestamp must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _result(*, item: dict[str, Any], run: dict[str, Any], created: bool) -> dict[str, Any]:
    return {
        "quarantine_id": str(item["quarantine_id"]),
        "raw_body_sha256": str(item["raw_body_sha256"]),
        "redrive_effect_id": str(item["redrive_effect_id"]),
        "run_id": str(run["id"]),
        "status": "redriven",
        "created": created,
    }
