"""Terminalize and remove one exact synthetic quarantine acceptance effect."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError
from psycopg.rows import dict_row

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "src"))

from exercise_quarantine_redrive import (  # noqa: E402
    ATTEMPT_LEASE,
    QuarantineAcceptanceError,
    load_redrive_handoff,
    require_exact_main_context,
    write_redacted_evidence,
)
from hindsight.db import connect  # noqa: E402
from hindsight.quarantine import (  # noqa: E402
    quarantine_table_from_env,
    validate_stored_quarantine_item,
)
from hindsight.quarantine_redrive import quarantine_redrive_binding  # noqa: E402
from hindsight.runtime import runtime_database_url  # noqa: E402
from hindsight.runs import claim_run_attempt, finish_run_attempt  # noqa: E402
from hindsight.tenant import tenant_scope  # noqa: E402

STORED_RECORD_KEYS = frozenset(
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        evidence = cleanup_quarantine_acceptance(
            handoff_path=pathlib.Path(args.handoff),
            output_path=pathlib.Path(args.output),
        )
    except QuarantineAcceptanceError as exc:
        print(f"quarantine acceptance cleanup refused: {exc}", file=sys.stderr)
        return 2
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "AwsClientError")
        print(f"quarantine acceptance cleanup AWS operation failed: {code}", file=sys.stderr)
        return 3
    except Exception:
        print("quarantine acceptance cleanup failed", file=sys.stderr)
        return 4
    print(json.dumps(evidence, sort_keys=True))
    return 0


def cleanup_quarantine_acceptance(
    *,
    handoff_path: pathlib.Path,
    output_path: pathlib.Path,
    table: Any | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    handoff = load_redrive_handoff(handoff_path)
    require_exact_main_context(handoff.source_revision)
    record = handoff.record
    binding = quarantine_redrive_binding(
        quarantine_id=str(record["quarantine_id"]),
        raw_body_sha256=str(record["raw_body_sha256"]),
    )
    resolved_table = table or quarantine_table_from_env()
    resolved_db_url = db_url or runtime_database_url()
    state = find_redrive_effect(
        db_url=resolved_db_url,
        tenant_id=str(record["tenant_id"]),
        source_run_id=str(record["run_id"]),
        idempotency_key=binding.idempotency_key,
    )
    if state is not None:
        _require_safe_target(state=state, handoff_record=record)
        state = _terminalize_target(
            state=state,
            db_url=resolved_db_url,
            tenant_id=str(record["tenant_id"]),
            source_run_id=str(record["run_id"]),
            idempotency_key=binding.idempotency_key,
        )
        _require_terminal_target(state)

    stored = _read_bound_item(
        table=resolved_table,
        handoff_record=record,
        binding=binding,
        expected_run_id=str(state["run"]["id"]) if state is not None else None,
    )
    if stored is not None and stored.get("status") == "redriven" and state is None:
        raise QuarantineAcceptanceError("redriven ledger item has no deterministic run effect")
    deleted = _delete_exact_item(table=resolved_table, item=stored) if stored is not None else False
    remaining = resolved_table.get_item(
        Key={"quarantine_id": binding.quarantine_id},
        ConsistentRead=True,
    ).get("Item")
    if remaining is not None:
        raise QuarantineAcceptanceError("synthetic quarantine record remains after cleanup")

    payload: dict[str, Any] = {
        "schema_version": 1,
        "source_revision": handoff.source_revision,
        "quarantine_id": binding.quarantine_id,
        "raw_body_sha256": binding.raw_body_sha256,
        "record_sha256": str(record["record_sha256"]),
        "redrive_binding_sha256": binding.binding_sha256,
        "redrive_effect_id": binding.effect_id,
        "source_run_id": str(record["run_id"]),
        "target_found": state is not None,
        "ledger_deleted": deleted,
        "ledger_absent": True,
    }
    if state is not None:
        payload.update(
            {
                "redriven_run_id": str(state["run"]["id"]),
                "effect_count": int(state["effect_count"]),
                "run_status": str(state["run"]["status"]),
                "decision_status": str(state["decision"]["status"]),
                "model_call_count": int(state["run"]["model_call_count"]),
                "cloudwatch_call_count": int(state["run"]["cloudwatch_call_count"]),
                "dispatch_status": str(state["dispatch"]["status"]),
                "dispatch_run_eligible": bool(state["dispatch"]["dispatcher_run_eligible"]),
                "dispatch_attempt_count": int(state["dispatch"]["attempt_count"]),
                "dispatch_delivery_attempt_count": int(state["dispatch"]["delivery_attempt_count"]),
                "dispatch_available_at": _timestamp(state["dispatch"]["available_at"]),
            }
        )
    return write_redacted_evidence(output_path, payload)


def find_redrive_effect(
    *,
    db_url: str,
    tenant_id: str,
    source_run_id: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    with tenant_scope(tenant_id):
        with connect(db_url, application_name="hindsight-quarantine-cleanup") as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT *, now() AS database_now FROM agent_runs WHERE id = %s",
                    (source_run_id,),
                )
                source = cur.fetchone()
                if source is None:
                    raise QuarantineAcceptanceError("quarantine source run does not exist")
                cur.execute(
                    "SELECT count(*) AS effect_count FROM agent_runs WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                effect_count = int(cur.fetchone()["effect_count"])
                if effect_count == 0:
                    return None
                if effect_count != 1:
                    raise QuarantineAcceptanceError("redrive cleanup found multiple effects")
                cur.execute(
                    "SELECT *, now() AS database_now FROM agent_runs WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                run = cur.fetchone()
                cur.execute(
                    """
                        SELECT dispatch.*, (
                            SELECT count(*) FROM agent_run_dispatch_attempts AS attempt
                            WHERE attempt.dispatch_id = dispatch.id
                        ) AS delivery_attempt_count,
                        EXISTS (
                            SELECT 1
                            FROM agent_runs AS eligible_run
                            WHERE eligible_run.id = dispatch.run_id
                                AND eligible_run.command_generation =
                                    dispatch.command_generation
                                AND (
                                    (
                                        dispatch.command = 'start'
                                        AND eligible_run.status = 'queued'
                                    )
                                    OR (
                                        dispatch.command = 'resume'
                                        AND eligible_run.status = 'resuming'
                                    )
                                    OR (
                                        eligible_run.status IN (
                                            'triaging', 'recalling', 'planning', 'reflecting'
                                        )
                                        AND eligible_run.worker_attempt_command =
                                            dispatch.command
                                        AND eligible_run.worker_attempt_generation =
                                            dispatch.command_generation
                                        AND eligible_run.worker_attempt_lease_expires_at <= now()
                                    )
                                )
                        ) AS dispatcher_run_eligible
                        FROM agent_run_dispatches AS dispatch
                        WHERE dispatch.run_id = %s
                            AND dispatch.command = 'start'
                            AND dispatch.command_generation = 0
                    """,
                    (run["id"],),
                )
                dispatches = cur.fetchall()
                cur.execute(
                    "SELECT status, sealed_at FROM memory_decisions WHERE id = %s",
                    (run["decision_id"],),
                )
                decision = cur.fetchone()
    if len(dispatches) != 1 or decision is None:
        raise QuarantineAcceptanceError("redrive cleanup target is incomplete")
    return {
        "effect_count": effect_count,
        "source": dict(source),
        "run": dict(run),
        "dispatch": dict(dispatches[0]),
        "decision": dict(decision),
    }


def _terminalize_target(
    *,
    state: dict[str, Any],
    db_url: str,
    tenant_id: str,
    source_run_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    for _ in range(3):
        run = state["run"]
        if run.get("status") == "rejected":
            return state
        attempt_id: str | None = None
        lease_expiry = run.get("worker_attempt_lease_expires_at")
        if (
            run.get("status") == "triaging"
            and run.get("worker_attempt_command") == "start"
            and int(run.get("worker_attempt_generation") or 0) == 0
            and run.get("worker_attempt_id") is not None
            and _is_aware_datetime(lease_expiry)
            and _is_aware_datetime(run.get("database_now"))
            and lease_expiry > run["database_now"]
        ):
            attempt_id = str(run["worker_attempt_id"])
        else:
            with tenant_scope(tenant_id):
                claim = claim_run_attempt(
                    run_id=str(run["id"]),
                    command="start",
                    command_generation=0,
                    lease_ttl=ATTEMPT_LEASE,
                    max_attempts=max(3, int(run.get("worker_attempt_count") or 0) + 1),
                    db_url=db_url,
                )
            if claim.outcome == "busy":
                state = find_redrive_effect(
                    db_url=db_url,
                    tenant_id=tenant_id,
                    source_run_id=source_run_id,
                    idempotency_key=idempotency_key,
                )
                if state is None:
                    raise QuarantineAcceptanceError("redrive cleanup target disappeared")
                continue
            if claim.outcome != "claimed" or claim.attempt_id is None:
                raise QuarantineAcceptanceError("redrive cleanup could not claim its target")
            attempt_id = claim.attempt_id
        with tenant_scope(tenant_id):
            finish_run_attempt(
                run_id=str(run["id"]),
                attempt_id=attempt_id,
                status="rejected",
                phase="rejection",
                summary="Run rejected before execution",
                command="start",
                fields={"action_approved": False},
                db_url=db_url,
            )
        refreshed = find_redrive_effect(
            db_url=db_url,
            tenant_id=tenant_id,
            source_run_id=source_run_id,
            idempotency_key=idempotency_key,
        )
        if refreshed is None:
            raise QuarantineAcceptanceError("redrive cleanup target disappeared")
        return refreshed
    raise QuarantineAcceptanceError("redrive cleanup target remained busy")


def _require_safe_target(*, state: dict[str, Any], handoff_record: dict[str, Any]) -> None:
    source = state["source"]
    run = state["run"]
    dispatch = state["dispatch"]
    database_now = run.get("database_now")
    available_at = dispatch.get("available_at")
    copied_fields = ("incident_slug", "namespace", "user_input", "service_slug", "retrieval_policy")
    if (
        str(source.get("id")) != str(handoff_record["run_id"])
        or source.get("status") != "failed"
        or source.get("failure_code") != "RunAttemptsExhausted"
        or source.get("worker_attempt_command") != handoff_record.get("command")
        or int(source.get("command_generation") or 0)
        != int(handoff_record.get("command_generation") or 0)
        or any(source.get(field) != run.get(field) for field in copied_fields)
        or run.get("status") not in {"queued", "triaging", "rejected"}
        or int(run.get("command_generation") or 0) != 0
        or int(run.get("model_call_count") or 0) != 0
        or int(run.get("cloudwatch_call_count") or 0) != 0
        or dispatch.get("status") != "pending"
        or int(dispatch.get("attempt_count") or 0) != 0
        or int(dispatch.get("delivery_attempt_count") or 0) != 0
        or not _is_aware_datetime(database_now)
        or not _is_aware_datetime(available_at)
        or available_at <= database_now + timedelta(days=1)
        or dispatch.get("lease_owner") is not None
        or dispatch.get("transport_message_id") is not None
        or dispatch.get("acknowledged_attempt_id") is not None
    ):
        raise QuarantineAcceptanceError("redrive cleanup refused an unsafe target")


def _require_terminal_target(state: dict[str, Any]) -> None:
    run = state["run"]
    decision = state["decision"]
    dispatch = state["dispatch"]
    database_now = run.get("database_now")
    available_at = dispatch.get("available_at")
    if (
        state.get("effect_count") != 1
        or run.get("status") != "rejected"
        or run.get("completed_at") is None
        or run.get("worker_attempt_id") is not None
        or run.get("worker_attempt_lease_expires_at") is not None
        or int(run.get("model_call_count") or 0) != 0
        or int(run.get("cloudwatch_call_count") or 0) != 0
        or decision.get("status") != "sealed"
        or decision.get("sealed_at") is None
        or dispatch.get("status") != "pending"
        or int(dispatch.get("attempt_count") or 0) != 0
        or int(dispatch.get("delivery_attempt_count") or 0) != 0
        or dispatch.get("dispatcher_run_eligible") is not False
        or not _is_aware_datetime(database_now)
        or not _is_aware_datetime(available_at)
        or available_at <= database_now + timedelta(days=1)
        or dispatch.get("lease_owner") is not None
        or dispatch.get("transport_message_id") is not None
        or dispatch.get("acknowledged_attempt_id") is not None
    ):
        raise QuarantineAcceptanceError("redrive cleanup did not leave a terminal safe target")


def _read_bound_item(
    *,
    table: Any,
    handoff_record: dict[str, Any],
    binding: Any,
    expected_run_id: str | None,
) -> dict[str, Any] | None:
    stored = table.get_item(
        Key={"quarantine_id": binding.quarantine_id},
        ConsistentRead=True,
    ).get("Item")
    if stored is None:
        return None
    try:
        item = validate_stored_quarantine_item(stored)
    except RuntimeError as exc:
        raise QuarantineAcceptanceError("quarantine cleanup item is invalid") from exc
    immutable = (
        "quarantine_id",
        "source_arn",
        "source_message_id",
        "raw_body_sha256",
        "command",
        "tenant_id",
        "run_id",
        "command_generation",
        "record_sha256",
    )
    if any(item.get(key) != handoff_record.get(key) for key in immutable):
        raise QuarantineAcceptanceError("quarantine cleanup identity conflicted")
    if item["status"] in {"redrive_pending", "redriven"} and (
        item.get("redrive_effect_id") != binding.effect_id
        or item.get("redrive_binding_sha256") != binding.binding_sha256
    ):
        raise QuarantineAcceptanceError("quarantine cleanup redrive binding conflicted")
    if item["status"] == "redriven" and (
        expected_run_id is None or item.get("redriven_run_id") != expected_run_id
    ):
        raise QuarantineAcceptanceError("quarantine cleanup run binding conflicted")
    return item


def _delete_exact_item(*, table: Any, item: dict[str, Any]) -> bool:
    condition = Attr("quarantine_id").eq(item["quarantine_id"])
    for key in sorted(STORED_RECORD_KEYS - {"quarantine_id"}):
        condition &= Attr(key).eq(item[key]) if key in item else Attr(key).not_exists()
    try:
        table.delete_item(
            Key={"quarantine_id": item["quarantine_id"]},
            ConditionExpression=condition,
        )
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        remaining = table.get_item(
            Key={"quarantine_id": item["quarantine_id"]},
            ConsistentRead=True,
        ).get("Item")
        if remaining is not None:
            raise QuarantineAcceptanceError(
                "quarantine cleanup record changed before delete"
            ) from None
        return False
    return True


def _timestamp(value: datetime) -> str:
    if not _is_aware_datetime(value):
        raise QuarantineAcceptanceError("database timestamp is invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _is_aware_datetime(value: Any) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


if __name__ == "__main__":
    sys.exit(main())
