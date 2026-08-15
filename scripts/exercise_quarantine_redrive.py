"""Exercise one exact quarantined-run redrive with a fenced synthetic effect."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import pathlib
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from botocore.exceptions import ClientError
from psycopg.rows import dict_row

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.db import connect  # noqa: E402
from hindsight.quarantine import (  # noqa: E402
    quarantine_table_from_env,
    stable_quarantine_id,
    validate_stored_quarantine_item,
)
from hindsight.quarantine_redrive import (  # noqa: E402
    QuarantineRedriveError,
    quarantine_redrive_binding,
    redrive_quarantined_run,
)
from hindsight.runtime import runtime_database_url  # noqa: E402
from hindsight.runs import claim_run_attempt, finish_run_attempt  # noqa: E402
from hindsight.tenant import tenant_scope  # noqa: E402

EXACT_SHA = re.compile(r"[0-9a-f]{40}")
EXACT_DIGEST = re.compile(r"[0-9a-f]{64}")
HANDOFF_KEYS = frozenset({"schema_version", "source_revision", "record", "payload_sha256"})
HANDOFF_RECORD_KEYS = frozenset(
    {
        "quarantine_id",
        "source_arn",
        "source_message_id",
        "raw_body_sha256",
        "record_sha256",
        "tenant_id",
        "run_id",
        "command",
        "command_generation",
        "status",
    }
)
FENCE_DURATION = timedelta(days=3650)
ATTEMPT_LEASE = timedelta(minutes=5)


class QuarantineAcceptanceError(RuntimeError):
    """Raised when synthetic redrive evidence is incomplete or conflicting."""


@dataclass(frozen=True)
class RedriveHandoff:
    source_revision: str
    record: dict[str, Any]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        evidence = exercise_quarantine_redrive(
            handoff_path=pathlib.Path(args.handoff),
            output_path=pathlib.Path(args.output),
        )
    except (QuarantineAcceptanceError, QuarantineRedriveError) as exc:
        print(f"quarantine redrive acceptance refused: {exc}", file=sys.stderr)
        return 2
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "AwsClientError")
        print(f"quarantine redrive acceptance AWS operation failed: {code}", file=sys.stderr)
        return 3
    except Exception:
        print("quarantine redrive acceptance failed", file=sys.stderr)
        return 4
    print(json.dumps(evidence, sort_keys=True))
    return 0


def exercise_quarantine_redrive(
    *,
    handoff_path: pathlib.Path,
    output_path: pathlib.Path,
    table: Any | None = None,
    db_url: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    handoff = load_redrive_handoff(handoff_path)
    repository_owner = require_exact_main_context(handoff.source_revision)
    record = handoff.record
    binding = quarantine_redrive_binding(
        quarantine_id=str(record["quarantine_id"]),
        raw_body_sha256=str(record["raw_body_sha256"]),
    )
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise QuarantineAcceptanceError("acceptance time must include a timezone")
    dispatch_available_at = current_time.astimezone(UTC) + FENCE_DURATION
    resolved_table = table or quarantine_table_from_env()
    _require_exact_handoff_item(table=resolved_table, handoff_record=record)
    resolved_db_url = db_url or runtime_database_url()
    arguments = {
        "table": resolved_table,
        "quarantine_id": binding.quarantine_id,
        "raw_body_sha256": binding.raw_body_sha256,
        "confirmation": binding.confirmation,
        "repository_owner": repository_owner,
        "actor": _required_environment("GITHUB_ACTOR"),
        "triggering_actor": _required_environment("GITHUB_TRIGGERING_ACTOR"),
        "db_url": resolved_db_url,
        "expected_record_sha256": str(record["record_sha256"]),
        "dispatch_available_at": dispatch_available_at,
        "now": current_time,
    }

    first = redrive_quarantined_run(**arguments)
    repeated = redrive_quarantined_run(**arguments)
    _require_one_effect(first=first, repeated=repeated, binding=binding)
    stored = _require_redriven_item(
        table=resolved_table,
        handoff_record=record,
        binding=binding,
        run_id=str(first["run_id"]),
    )
    before = inspect_redrive_effect(
        db_url=resolved_db_url,
        tenant_id=str(record["tenant_id"]),
        idempotency_key=binding.idempotency_key,
    )
    _require_fenced_target(
        before,
        expected_run_id=str(first["run_id"]),
        dispatch_available_at=dispatch_available_at,
    )

    with tenant_scope(str(record["tenant_id"])):
        claim = claim_run_attempt(
            run_id=str(first["run_id"]),
            command="start",
            command_generation=0,
            lease_ttl=ATTEMPT_LEASE,
            max_attempts=3,
            db_url=resolved_db_url,
        )
        if claim.outcome != "claimed" or claim.attempt_id is None:
            raise QuarantineAcceptanceError("fenced redrive target could not be claimed")
        finish_run_attempt(
            run_id=str(first["run_id"]),
            attempt_id=claim.attempt_id,
            status="rejected",
            phase="rejection",
            summary="Run rejected before execution",
            command="start",
            fields={"action_approved": False},
            db_url=resolved_db_url,
        )

    after = inspect_redrive_effect(
        db_url=resolved_db_url,
        tenant_id=str(record["tenant_id"]),
        idempotency_key=binding.idempotency_key,
    )
    _require_terminal_target(
        after,
        expected_run_id=str(first["run_id"]),
        dispatch_available_at=dispatch_available_at,
    )
    payload = {
        "schema_version": 1,
        "source_revision": handoff.source_revision,
        "quarantine_id": binding.quarantine_id,
        "raw_body_sha256": binding.raw_body_sha256,
        "record_sha256": str(stored["record_sha256"]),
        "redrive_binding_sha256": binding.binding_sha256,
        "redrive_effect_id": binding.effect_id,
        "confirmation_sha256": hashlib.sha256(binding.confirmation.encode("utf-8")).hexdigest(),
        "source_run_id": str(record["run_id"]),
        "redriven_run_id": str(first["run_id"]),
        "first_created": True,
        "repeat_created": False,
        "effect_count": int(after["effect_count"]),
        "run_status": str(after["run"]["status"]),
        "decision_status": str(after["decision"]["status"]),
        "model_call_count": int(after["run"]["model_call_count"]),
        "cloudwatch_call_count": int(after["run"]["cloudwatch_call_count"]),
        "dispatch_status": str(after["dispatch"]["status"]),
        "dispatch_run_eligible": bool(after["dispatch"]["dispatcher_run_eligible"]),
        "dispatch_attempt_count": int(after["dispatch"]["attempt_count"]),
        "dispatch_delivery_attempt_count": int(after["dispatch"]["delivery_attempt_count"]),
        "dispatch_available_at": _timestamp(after["dispatch"]["available_at"]),
    }
    return write_redacted_evidence(output_path, payload)


def load_redrive_handoff(path: pathlib.Path) -> RedriveHandoff:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.stat().st_size > 1_000_000:
        raise QuarantineAcceptanceError("redrive handoff is unavailable or oversized")
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QuarantineAcceptanceError("redrive handoff is not valid JSON") from exc
    if not isinstance(document, dict) or set(document) != HANDOFF_KEYS:
        raise QuarantineAcceptanceError("redrive handoff schema is invalid")
    if document.get("schema_version") != 1:
        raise QuarantineAcceptanceError("redrive handoff version is unsupported")
    source_revision = str(document.get("source_revision") or "")
    if EXACT_SHA.fullmatch(source_revision) is None:
        raise QuarantineAcceptanceError("redrive handoff revision is invalid")
    supplied_digest = str(document.get("payload_sha256") or "")
    payload = {key: document[key] for key in ("schema_version", "source_revision", "record")}
    expected_digest = hashlib.sha256(_canonical_bytes(payload)).hexdigest()
    if not hmac.compare_digest(supplied_digest, expected_digest):
        raise QuarantineAcceptanceError("redrive handoff digest does not match")
    record = _validate_handoff_record(document.get("record"))
    return RedriveHandoff(source_revision=source_revision, record=record)


def _validate_handoff_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != HANDOFF_RECORD_KEYS:
        raise QuarantineAcceptanceError("redrive handoff record schema is invalid")
    record = dict(value)
    if record.get("status") != "quarantined" or record.get("command") not in {
        "start",
        "resume",
    }:
        raise QuarantineAcceptanceError("redrive handoff record is not eligible")
    for key in (
        "quarantine_id",
        "source_arn",
        "source_message_id",
        "raw_body_sha256",
        "record_sha256",
        "tenant_id",
        "run_id",
    ):
        if not isinstance(record.get(key), str) or not record[key]:
            raise QuarantineAcceptanceError("redrive handoff record identity is invalid")
    try:
        expected_id = stable_quarantine_id(
            source_arn=record["source_arn"],
            source_message_id=record["source_message_id"],
        )
        tenant_id = str(UUID(record["tenant_id"]))
        run_id = str(UUID(record["run_id"]))
    except (RuntimeError, ValueError, AttributeError) as exc:
        raise QuarantineAcceptanceError("redrive handoff record identity is invalid") from exc
    if (
        record["quarantine_id"] != expected_id
        or EXACT_DIGEST.fullmatch(record["raw_body_sha256"]) is None
        or EXACT_DIGEST.fullmatch(record["record_sha256"]) is None
        or tenant_id != record["tenant_id"]
        or run_id != record["run_id"]
        or type(record.get("command_generation")) is not int
        or not 0 <= record["command_generation"] <= 2**63 - 1
    ):
        raise QuarantineAcceptanceError("redrive handoff record identity is invalid")
    return record


def require_exact_main_context(source_revision: str) -> str:
    repository = _required_environment("GITHUB_REPOSITORY")
    repository_owner, separator, repository_name = repository.partition("/")
    if not separator or not repository_owner or not repository_name:
        raise QuarantineAcceptanceError("GitHub repository identity is invalid")
    if _required_environment("GITHUB_REPOSITORY_OWNER") != repository_owner:
        raise QuarantineAcceptanceError("GitHub repository owner does not match")
    if (
        _required_environment("GITHUB_ACTOR") != repository_owner
        or _required_environment("GITHUB_TRIGGERING_ACTOR") != repository_owner
    ):
        raise QuarantineAcceptanceError("redrive acceptance requires the repository owner")
    if _required_environment("GITHUB_REF") != "refs/heads/main":
        raise QuarantineAcceptanceError("redrive acceptance requires exact main")
    if _required_environment("GITHUB_REF_PROTECTED").lower() != "true":
        raise QuarantineAcceptanceError("redrive acceptance requires protected main")
    github_sha = _required_environment("GITHUB_SHA")
    acceptance_sha = _required_environment("HINDSIGHT_ACCEPTANCE_SOURCE_REVISION")
    if github_sha != source_revision or acceptance_sha != source_revision:
        raise QuarantineAcceptanceError("redrive acceptance revision does not match")
    return repository_owner


def inspect_redrive_effect(
    *,
    db_url: str,
    tenant_id: str,
    idempotency_key: str,
) -> dict[str, Any]:
    with tenant_scope(tenant_id):
        with connect(db_url, application_name="hindsight-quarantine-acceptance") as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT count(*) AS effect_count FROM agent_runs WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                effect_count = int(cur.fetchone()["effect_count"])
                cur.execute(
                    "SELECT * FROM agent_runs WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                run = cur.fetchone()
                if run is None:
                    raise QuarantineAcceptanceError("redrive target run does not exist")
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
    if effect_count != 1 or len(dispatches) != 1 or decision is None:
        raise QuarantineAcceptanceError("redrive target effect is not unique and complete")
    return {
        "effect_count": effect_count,
        "run": dict(run),
        "dispatch": dict(dispatches[0]),
        "decision": dict(decision),
    }


def write_redacted_evidence(path: pathlib.Path, payload: dict[str, Any]) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.parent.is_dir():
        raise QuarantineAcceptanceError("redrive evidence directory does not exist")
    evidence = {
        **payload,
        "payload_sha256": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
    }
    resolved.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return evidence


def _require_one_effect(*, first: dict[str, Any], repeated: dict[str, Any], binding: Any) -> None:
    keys = ("quarantine_id", "raw_body_sha256", "redrive_effect_id", "run_id", "status")
    if first.get("created") is not True or repeated.get("created") is not False:
        raise QuarantineAcceptanceError("redrive did not create exactly one effect")
    if any(first.get(key) != repeated.get(key) for key in keys):
        raise QuarantineAcceptanceError("redrive repeat did not resolve the same effect")
    if (
        first.get("quarantine_id") != binding.quarantine_id
        or first.get("raw_body_sha256") != binding.raw_body_sha256
        or first.get("redrive_effect_id") != binding.effect_id
        or first.get("status") != "redriven"
    ):
        raise QuarantineAcceptanceError("redrive result identity does not match")


def _require_exact_handoff_item(*, table: Any, handoff_record: dict[str, Any]) -> None:
    stored = table.get_item(
        Key={"quarantine_id": handoff_record["quarantine_id"]},
        ConsistentRead=True,
    ).get("Item")
    try:
        item = validate_stored_quarantine_item(stored)
        observed = {key: item[key] for key in HANDOFF_RECORD_KEYS}
    except (KeyError, RuntimeError) as exc:
        raise QuarantineAcceptanceError("redrive handoff record is unavailable") from exc
    if observed != handoff_record:
        raise QuarantineAcceptanceError("redrive handoff does not match the live record")


def _require_redriven_item(
    *,
    table: Any,
    handoff_record: dict[str, Any],
    binding: Any,
    run_id: str,
) -> dict[str, Any]:
    stored = table.get_item(
        Key={"quarantine_id": binding.quarantine_id},
        ConsistentRead=True,
    ).get("Item")
    try:
        item = validate_stored_quarantine_item(stored)
    except RuntimeError as exc:
        raise QuarantineAcceptanceError("redriven quarantine record is unavailable") from exc
    immutable = (
        "quarantine_id",
        "source_arn",
        "source_message_id",
        "raw_body_sha256",
        "record_sha256",
        "tenant_id",
        "run_id",
        "command",
        "command_generation",
    )
    if any(item.get(key) != handoff_record.get(key) for key in immutable):
        raise QuarantineAcceptanceError("redriven quarantine identity conflicted")
    if (
        item.get("status") != "redriven"
        or item.get("redrive_effect_id") != binding.effect_id
        or item.get("redrive_binding_sha256") != binding.binding_sha256
        or item.get("redriven_run_id") != run_id
    ):
        raise QuarantineAcceptanceError("redriven quarantine state conflicted")
    return item


def _require_fenced_target(
    state: dict[str, Any],
    *,
    expected_run_id: str,
    dispatch_available_at: datetime,
) -> None:
    run = state["run"]
    dispatch = state["dispatch"]
    available_at = dispatch.get("available_at")
    if (
        str(run.get("id")) != expected_run_id
        or run.get("status") != "queued"
        or int(run.get("model_call_count") or 0) != 0
        or int(run.get("cloudwatch_call_count") or 0) != 0
        or dispatch.get("status") != "pending"
        or int(dispatch.get("attempt_count") or 0) != 0
        or int(dispatch.get("delivery_attempt_count") or 0) != 0
        or dispatch.get("dispatcher_run_eligible") is not True
        or not _is_aware_datetime(available_at)
        or available_at < dispatch_available_at
        or dispatch.get("lease_owner") is not None
        or dispatch.get("transport_message_id") is not None
        or dispatch.get("acknowledged_attempt_id") is not None
    ):
        raise QuarantineAcceptanceError("redrive target was not atomically fenced")


def _require_terminal_target(
    state: dict[str, Any],
    *,
    expected_run_id: str,
    dispatch_available_at: datetime,
) -> None:
    run = state["run"]
    dispatch = state["dispatch"]
    decision = state["decision"]
    available_at = dispatch.get("available_at")
    if (
        state.get("effect_count") != 1
        or str(run.get("id")) != expected_run_id
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
        or not _is_aware_datetime(available_at)
        or available_at < dispatch_available_at
        or dispatch.get("lease_owner") is not None
        or dispatch.get("transport_message_id") is not None
        or dispatch.get("acknowledged_attempt_id") is not None
    ):
        raise QuarantineAcceptanceError("redrive target did not close without external work")


def _required_environment(name: str) -> str:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        raise QuarantineAcceptanceError(f"{name} is required")
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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
