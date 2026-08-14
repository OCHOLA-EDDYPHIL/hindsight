"""Owner-confirmed, idempotent redrive for quarantined run work."""

from __future__ import annotations

import hashlib
import hmac
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
    now: datetime | None = None,
) -> dict[str, Any]:
    """Create one fresh run from an exhausted source under an exact owner gate."""

    _require_owner_gate(
        repository_owner=repository_owner,
        actor=actor,
        triggering_actor=triggering_actor,
    )
    normalized_id = _quarantine_id(quarantine_id)
    normalized_digest = _digest(raw_body_sha256)
    expected_confirmation = f"redrive:{normalized_id}:{normalized_digest}"
    if not hmac.compare_digest(confirmation, expected_confirmation):
        raise QuarantineRedriveError("redrive confirmation phrase does not match")
    binding_sha256 = hashlib.sha256(
        f"{normalized_id}:{normalized_digest}".encode("utf-8")
    ).hexdigest()
    effect_id = str(
        uuid5(
            NAMESPACE_URL,
            f"hindsight:quarantine-redrive:{normalized_id}:{normalized_digest}",
        )
    )

    item = _get_item(table=table, quarantine_id=normalized_id)
    if not hmac.compare_digest(str(item["raw_body_sha256"]), normalized_digest):
        raise QuarantineRedriveError("quarantine body digest does not match")
    _require_run_item(item)
    item = _claim_redrive(
        table=table,
        item=item,
        effect_id=effect_id,
        binding_sha256=binding_sha256,
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
                idempotency_key=f"quarantine-redrive:{effect_id}",
                db_url=db_url,
            )
        except (RunConflictError, RunIdempotencyConflictError, RunNotFoundError, ValueError) as exc:
            raise QuarantineRedriveError("quarantined source run cannot be redriven") from exc
    completed_at = _timestamp(now or datetime.now(UTC))
    try:
        response = table.update_item(
            Key={"quarantine_id": normalized_id},
            UpdateExpression=(
                "SET #status = :redriven, redriven_at = if_not_exists(redriven_at, :at), "
                "redriven_run_id = if_not_exists(redriven_run_id, :run_id)"
            ),
            ConditionExpression=(
                Attr("status").eq("redrive_pending")
                & Attr("redrive_effect_id").eq(effect_id)
                & Attr("redrive_binding_sha256").eq(binding_sha256)
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
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        completed = _get_item(table=table, quarantine_id=normalized_id)
        if (
            completed.get("status") != "redriven"
            or completed.get("redrive_effect_id") != effect_id
            or completed.get("redrive_binding_sha256") != binding_sha256
            or completed.get("redriven_run_id") != str(run["id"])
        ):
            raise QuarantineRedriveError("quarantine redrive completion conflicted") from None
    return _result(item=completed, run=run, created=created)


def _claim_redrive(
    *,
    table: Any,
    item: dict[str, Any],
    effect_id: str,
    binding_sha256: str,
    now: datetime | None,
) -> dict[str, Any]:
    status = item["status"]
    if status in {"redrive_pending", "redriven"}:
        if (
            item.get("redrive_effect_id") != effect_id
            or item.get("redrive_binding_sha256") != binding_sha256
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
            ConditionExpression=Attr("status").eq("quarantined"),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":pending": "redrive_pending",
                ":effect_id": effect_id,
                ":binding_sha256": binding_sha256,
                ":started_at": started_at,
            },
            ReturnValues="ALL_NEW",
        )
        return validate_stored_quarantine_item(response.get("Attributes"))
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raced = _get_item(table=table, quarantine_id=str(item["quarantine_id"]))
        if (
            raced.get("status") not in {"redrive_pending", "redriven"}
            or raced.get("redrive_effect_id") != effect_id
            or raced.get("redrive_binding_sha256") != binding_sha256
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
