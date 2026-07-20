"""Durable, archive-first authority for one frozen learning protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.db import connect
from hindsight.evidence_archive import EvidenceArchive, canonical_json_bytes, sha256_hex
from hindsight.server_tenants import learning_tenant_id
from hindsight.tenant import tenant_scope

PROTOCOL_SLOT = "protocol-v3-reset-1"
PROTOCOL_KEY = f"learning/authority/{PROTOCOL_SLOT}/authorization.json"
PRODUCT_PROVENANCE_KEY = f"learning/authority/{PROTOCOL_SLOT}/product-provenance.json"
LEARNING_WORKFLOW = "learning evidence"


def execution_key(sequence: int, name: str) -> str:
    _require_sequence(sequence)
    if name not in {"authorization", "consumption", "finalization"}:
        raise ValueError("invalid execution authority object")
    return f"learning/authority/{PROTOCOL_SLOT}/execution-{sequence}/{name}.json"


def canonical_sha256(payload: Any) -> str:
    return sha256_hex(canonical_json_bytes(payload))


def protocol_authorization_id() -> str:
    return str(uuid5(NAMESPACE_URL, f"https://hindsight.local/learning/{PROTOCOL_SLOT}"))


def execution_authorization_id(sequence: int) -> str:
    _require_sequence(sequence)
    return str(
        uuid5(
            UUID(protocol_authorization_id()),
            f"execution-{sequence}",
        )
    )


def authorize_protocol(
    *,
    archive: EvidenceArchive,
    db_url: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Create the one fixed reset authority and mirror it into the current database."""

    _validate_protocol_payload(payload)
    record = archive.put_canonical_json(key=PROTOCOL_KEY, payload=payload)
    _mirror_protocol(db_url=db_url, payload=payload, record=record, bucket=archive.bucket)
    return {"payload": payload, "record": record}


def authorize_execution(
    *,
    archive: EvidenceArchive,
    db_url: str,
    sequence: int,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Create one sequence authority after reconciling its protocol dependency."""

    _require_sequence(sequence)
    protocol = _load_protocol(archive=archive, db_url=db_url)
    _validate_execution_payload(
        payload,
        sequence=sequence,
        protocol_sha256=protocol["record"]["sha256"],
    )
    if sequence == 2:
        previous = archive.get_canonical_json_if_exists(
            key=execution_key(1, "finalization")
        )
        if previous is None:
            raise RuntimeError("sequence two requires a sealed sequence-one finalization")
        previous_payload, previous_record = previous
        if previous_payload.get("terminal_class") != "infrastructure_outcome_free":
            raise RuntimeError("sequence two is limited to outcome-free infrastructure recovery")
        if payload.get("previous_finalization_sha256") != previous_record["sha256"]:
            raise RuntimeError("sequence-two authority is not bound to sequence one")
    key = execution_key(sequence, "authorization")
    record = archive.put_canonical_json(key=key, payload=payload)
    _mirror_execution_authorization(
        db_url=db_url,
        payload=payload,
        record=record,
    )
    return {"payload": payload, "record": record}


def consume_execution(
    *,
    archive: EvidenceArchive,
    db_url: str,
    sequence: int,
    workflow_run_id: int,
    workflow_run_attempt: int,
    code_sha: str,
) -> dict[str, Any]:
    """Claim an execution globally, then reconcile the claim into the database."""

    protocol = _load_protocol(archive=archive, db_url=db_url)
    execution = _load_execution_authorization(
        archive=archive,
        db_url=db_url,
        sequence=sequence,
    )
    if code_sha != protocol["payload"]["code_sha"]:
        raise RuntimeError("execution revision differs from protocol authorization")
    payload = {
        "schema_version": 1,
        "protocol_authorization_id": protocol_authorization_id(),
        "protocol_authorization_sha256": protocol["record"]["sha256"],
        "execution_authorization_id": execution_authorization_id(sequence),
        "execution_authorization_sha256": execution["record"]["sha256"],
        "sequence": sequence,
        "workflow_name": LEARNING_WORKFLOW,
        "workflow_run_id": _positive_int(workflow_run_id, "workflow run id"),
        "workflow_run_attempt": _positive_int(
            workflow_run_attempt,
            "workflow run attempt",
        ),
        "code_sha": code_sha,
    }
    key = execution_key(sequence, "consumption")
    record = archive.put_canonical_json(key=key, payload=payload)
    _mirror_consumption(db_url=db_url, payload=payload, record=record)
    require_execution_lease(
        db_url=db_url,
        sequence=sequence,
        workflow_run_id=workflow_run_id,
        workflow_run_attempt=workflow_run_attempt,
        code_sha=code_sha,
        protocol_authorization_sha256=protocol["record"]["sha256"],
    )
    return {"payload": payload, "record": record}


def reconcile_consumed_execution(
    *,
    archive: EvidenceArchive,
    db_url: str,
    sequence: int,
) -> dict[str, Any]:
    """Rebuild a consumed lease from immutable objects after database replacement."""

    protocol = _load_protocol(archive=archive, db_url=db_url)
    execution = _load_execution_authorization(
        archive=archive,
        db_url=db_url,
        sequence=sequence,
    )
    consumed = archive.get_canonical_json_if_exists(key=execution_key(sequence, "consumption"))
    if consumed is None:
        raise RuntimeError("learning execution has not been consumed")
    payload, record = consumed
    if (
        payload.get("protocol_authorization_sha256") != protocol["record"]["sha256"]
        or payload.get("execution_authorization_sha256") != execution["record"]["sha256"]
    ):
        raise RuntimeError("consumption identity differs from immutable authority")
    _mirror_consumption(db_url=db_url, payload=payload, record=record)
    require_execution_lease(
        db_url=db_url,
        sequence=sequence,
        workflow_run_id=int(payload["workflow_run_id"]),
        workflow_run_attempt=int(payload["workflow_run_attempt"]),
        code_sha=str(payload["code_sha"]),
        protocol_authorization_sha256=str(payload["protocol_authorization_sha256"]),
    )
    return {
        "protocol_authorization_sha256": protocol["record"]["sha256"],
        "execution_authorization_sha256": execution["record"]["sha256"],
        "workflow_run_id": payload["workflow_run_id"],
        "workflow_run_attempt": payload["workflow_run_attempt"],
        "code_sha": payload["code_sha"],
        "sequence": sequence,
    }


def require_execution_lease(
    *,
    db_url: str,
    sequence: int,
    workflow_run_id: int,
    workflow_run_attempt: int,
    code_sha: str,
    protocol_authorization_sha256: str,
) -> dict[str, Any]:
    """Require one consumed lease to match the exact workflow attempt and reset."""

    with tenant_scope(learning_tenant_id()):
        with connect(db_url, application_name="hindsight-learning-authority") as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        SELECT execution.*,
                            protocol.authorization_sha256 AS protocol_authorization_sha256,
                            protocol.protocol_identity_sha256
                        FROM learning_execution_authorizations AS execution
                        JOIN learning_protocol_authorizations AS protocol
                          ON protocol.tenant_id = execution.tenant_id
                         AND protocol.id = execution.protocol_authorization_id
                        WHERE execution.id = %s
                    """,
                    (execution_authorization_id(sequence),),
                )
                row = cur.fetchone()
    if row is None:
        raise RuntimeError("learning execution authority is absent")
    expected = {
        "consumer_workflow_run_id": _positive_int(workflow_run_id, "workflow run id"),
        "consumer_workflow_run_attempt": _positive_int(
            workflow_run_attempt,
            "workflow run attempt",
        ),
        "consumer_code_sha": code_sha,
        "protocol_authorization_sha256": protocol_authorization_sha256,
    }
    if row["status"] not in {"consumed", "finalized"} or any(
        str(row[field]) != str(value) for field, value in expected.items()
    ):
        raise RuntimeError("learning execution lease identity mismatch")
    return dict(row)


def seal_execution(
    *,
    archive: EvidenceArchive,
    db_url: str,
    sequence: int,
    study_report: dict[str, Any],
) -> dict[str, Any]:
    """Seal one classified study and finalize only its consumed execution."""

    execution = _load_execution_authorization(
        archive=archive,
        db_url=db_url,
        sequence=sequence,
    )
    consumed = archive.get_canonical_json_if_exists(key=execution_key(sequence, "consumption"))
    if consumed is None:
        raise RuntimeError("cannot seal an unconsumed execution")
    consumption_payload, consumption_record = consumed
    _mirror_consumption(
        db_url=db_url,
        payload=consumption_payload,
        record=consumption_record,
    )
    _validate_study_report(
        study_report,
        sequence=sequence,
        consumption=consumption_payload,
    )
    receipt = archive.seal_bundle(
        evidence_id=f"study/{PROTOCOL_SLOT}/execution-{sequence}",
        objects={"study": study_report},
        dependencies={
            "execution_authorization_sha256": execution["record"]["sha256"],
            "execution_consumption_sha256": consumption_record["sha256"],
            "protocol_authorization_sha256": consumption_payload[
                "protocol_authorization_sha256"
            ],
        },
    )
    finalization = {
        "schema_version": 1,
        "protocol_authorization_id": protocol_authorization_id(),
        "protocol_authorization_sha256": consumption_payload[
            "protocol_authorization_sha256"
        ],
        "execution_authorization_id": execution_authorization_id(sequence),
        "execution_authorization_sha256": execution["record"]["sha256"],
        "consumption_sha256": consumption_record["sha256"],
        "sequence": sequence,
        "workflow_name": LEARNING_WORKFLOW,
        "workflow_run_id": consumption_payload["workflow_run_id"],
        "workflow_run_attempt": consumption_payload["workflow_run_attempt"],
        "code_sha": consumption_payload["code_sha"],
        "result": study_report["result"],
        "protocol_valid": study_report["protocol_valid"],
        "reason_code": study_report["reason_code"],
        "terminal_class": study_report["terminal_class"],
        "terminal_reason": study_report["terminal_reason"],
        "canonical_report_sha256": canonical_sha256(study_report),
        "evidence_receipt": receipt,
    }
    finalization_record = archive.put_canonical_json(
        key=execution_key(sequence, "finalization"),
        payload=finalization,
    )
    _mirror_finalization(
        db_url=db_url,
        finalization=finalization,
        finalization_record=finalization_record,
        study_report=study_report,
    )
    return {
        "result": study_report["result"],
        "terminal_class": study_report["terminal_class"],
        "receipt": receipt,
        "finalization": finalization_record,
    }


def _load_protocol(*, archive: EvidenceArchive, db_url: str) -> dict[str, Any]:
    payload, record = archive.get_canonical_json(key=PROTOCOL_KEY)
    _validate_protocol_payload(payload)
    _mirror_protocol(db_url=db_url, payload=payload, record=record, bucket=archive.bucket)
    return {"payload": payload, "record": record}


def _load_execution_authorization(
    *, archive: EvidenceArchive, db_url: str, sequence: int
) -> dict[str, Any]:
    protocol = _load_protocol(archive=archive, db_url=db_url)
    payload, record = archive.get_canonical_json(
        key=execution_key(sequence, "authorization")
    )
    _validate_execution_payload(
        payload,
        sequence=sequence,
        protocol_sha256=protocol["record"]["sha256"],
    )
    _mirror_execution_authorization(db_url=db_url, payload=payload, record=record)
    return {"payload": payload, "record": record}


def _mirror_protocol(
    *, db_url: str, payload: dict[str, Any], record: dict[str, Any], bucket: str
) -> None:
    with tenant_scope(learning_tenant_id()):
        with connect(db_url, application_name="hindsight-learning-authority") as conn:
            conn.execute(
                """
                    INSERT INTO learning_protocol_authorizations (
                        id, authorization_slot, authorization_payload,
                        authorization_sha256, protocol_schema_version,
                        protocol_identity_sha256, corpus_sha256, code_sha,
                        reasoning_provider, reasoning_model,
                        embedding_profile_id, embedding_provider, embedding_model,
                        embedding_max_distance, qualification_run_id,
                        qualification_evidence_sha256, product_run_id,
                        product_provenance_sha256, authorized_by,
                        authorization_workflow_run_id,
                        authorization_workflow_run_attempt, archive_bucket,
                        archive_key, archive_version_id, archive_sha256
                    ) VALUES (
                        %s, %s, %s, %s, 3, %s, %s, %s, %s, %s, %s, %s, %s,
                        0.35, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    ) ON CONFLICT (id) DO NOTHING
                """,
                (
                    protocol_authorization_id(),
                    PROTOCOL_SLOT,
                    Jsonb(payload),
                    record["sha256"],
                    payload["protocol_identity_sha256"],
                    payload["corpus_sha256"],
                    payload["code_sha"],
                    payload["reasoning_provider"],
                    payload["reasoning_model"],
                    payload["embedding_profile_id"],
                    payload["embedding_provider"],
                    payload["embedding_model"],
                    payload["qualification_run_id"],
                    payload["qualification_evidence_sha256"],
                    payload["product_run_id"],
                    payload["product_provenance_sha256"],
                    payload["authorized_by"],
                    payload["authorization_workflow_run_id"],
                    payload["authorization_workflow_run_attempt"],
                    bucket,
                    record["key"],
                    record["version_id"],
                    record["sha256"],
                ),
            )
            row = conn.execute(
                "SELECT authorization_sha256 FROM learning_protocol_authorizations WHERE id = %s",
                (protocol_authorization_id(),),
            ).fetchone()
            conn.commit()
    if row != (record["sha256"],):
        raise RuntimeError("database protocol authority differs from immutable archive")


def _mirror_execution_authorization(
    *, db_url: str, payload: dict[str, Any], record: dict[str, Any]
) -> None:
    sequence = int(payload["sequence"])
    with tenant_scope(learning_tenant_id()):
        with connect(db_url, application_name="hindsight-learning-authority") as conn:
            conn.execute(
                """
                    INSERT INTO learning_execution_authorizations (
                        id, protocol_authorization_id, sequence,
                        authorization_payload, authorization_sha256,
                        authorization_workflow_run_id,
                        authorization_workflow_run_attempt,
                        authorization_archive_key,
                        authorization_archive_version_id,
                        authorization_archive_sha256
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                """,
                (
                    execution_authorization_id(sequence),
                    protocol_authorization_id(),
                    sequence,
                    Jsonb(payload),
                    record["sha256"],
                    payload["authorization_workflow_run_id"],
                    payload["authorization_workflow_run_attempt"],
                    record["key"],
                    record["version_id"],
                    record["sha256"],
                ),
            )
            row = conn.execute(
                "SELECT authorization_sha256 FROM learning_execution_authorizations WHERE id = %s",
                (execution_authorization_id(sequence),),
            ).fetchone()
            conn.commit()
    if row != (record["sha256"],):
        raise RuntimeError("database execution authority differs from immutable archive")


def _mirror_consumption(
    *, db_url: str, payload: dict[str, Any], record: dict[str, Any]
) -> None:
    sequence = int(payload["sequence"])
    with tenant_scope(learning_tenant_id()):
        with connect(db_url, application_name="hindsight-learning-authority") as conn:
            conn.execute(
                """
                    UPDATE learning_execution_authorizations
                    SET status = 'consumed', consumer_workflow_run_id = %s,
                        consumer_workflow_run_attempt = %s, consumer_code_sha = %s,
                        consumption_payload = %s, consumption_sha256 = %s,
                        consumption_archive_key = %s,
                        consumption_archive_version_id = %s, consumed_at = now()
                    WHERE id = %s AND status = 'ready'
                """,
                (
                    payload["workflow_run_id"],
                    payload["workflow_run_attempt"],
                    payload["code_sha"],
                    Jsonb(payload),
                    record["sha256"],
                    record["key"],
                    record["version_id"],
                    execution_authorization_id(sequence),
                ),
            )
            row = conn.execute(
                """
                    SELECT status, consumption_sha256,
                        consumer_workflow_run_id, consumer_workflow_run_attempt
                    FROM learning_execution_authorizations WHERE id = %s
                """,
                (execution_authorization_id(sequence),),
            ).fetchone()
            conn.commit()
    expected = (
        "consumed",
        record["sha256"],
        payload["workflow_run_id"],
        payload["workflow_run_attempt"],
    )
    finalized_expected = ("finalized", *expected[1:])
    if row not in {expected, finalized_expected}:
        raise RuntimeError("database execution consumption differs from immutable archive")


def _mirror_finalization(
    *,
    db_url: str,
    finalization: dict[str, Any],
    finalization_record: dict[str, Any],
    study_report: dict[str, Any],
) -> None:
    sequence = int(finalization["sequence"])
    receipt = finalization["evidence_receipt"]
    evidence_id = str(
        uuid5(UUID(execution_authorization_id(sequence)), receipt["manifest_sha256"])
    )
    report_bytes = canonical_json_bytes(study_report)
    retain_until = datetime.fromisoformat(receipt["retain_until"])
    with tenant_scope(learning_tenant_id()):
        with connect(db_url, application_name="hindsight-learning-authority") as conn:
            with conn.transaction():
                conn.execute(
                    """
                        INSERT INTO learning_evidence_records (
                            id, evidence_kind, result, protocol_valid, reason_code,
                            code_sha, protocol_identity_sha256,
                            protocol_authorization_id, execution_authorization_id,
                            workflow_name, workflow_run_id, workflow_run_attempt,
                            canonical_report, canonical_report_sha256, archive_bucket,
                            manifest_key, manifest_version_id, manifest_sha256,
                            retain_until
                        ) VALUES (
                            %s, 'study', %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        ) ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        evidence_id,
                        finalization["result"],
                        finalization["protocol_valid"],
                        finalization["reason_code"],
                        finalization["code_sha"],
                        study_report["protocol_identity_sha256"],
                        protocol_authorization_id(),
                        execution_authorization_id(sequence),
                        finalization["workflow_name"],
                        finalization["workflow_run_id"],
                        finalization["workflow_run_attempt"],
                        report_bytes,
                        finalization["canonical_report_sha256"],
                        receipt["bucket"],
                        receipt["manifest_key"],
                        receipt["manifest_version_id"],
                        receipt["manifest_sha256"],
                        retain_until,
                    ),
                )
                conn.execute(
                    """
                        UPDATE learning_execution_authorizations
                        SET status = 'finalized', terminal_class = %s,
                            terminal_reason = %s, terminal_evidence_sha256 = %s,
                            finalized_at = now()
                        WHERE id = %s AND status = 'consumed'
                    """,
                    (
                        finalization["terminal_class"],
                        finalization["terminal_reason"],
                        finalization_record["sha256"],
                        execution_authorization_id(sequence),
                    ),
                )
                row = conn.execute(
                    """
                        SELECT status, terminal_evidence_sha256
                        FROM learning_execution_authorizations WHERE id = %s
                    """,
                    (execution_authorization_id(sequence),),
                ).fetchone()
    if row != ("finalized", finalization_record["sha256"]):
        raise RuntimeError("database finalization differs from immutable archive")


def _validate_protocol_payload(payload: dict[str, Any]) -> None:
    required = {
        "schema_version": 1,
        "authorization_slot": PROTOCOL_SLOT,
        "protocol_authorization_id": protocol_authorization_id(),
        "protocol_schema_version": 3,
        "reasoning_provider": "gemini",
        "reasoning_model": "gemini-3.1-flash-lite",
        "embedding_provider": "gemini",
        "embedding_model": "gemini-embedding-2",
        "embedding_max_distance": 0.35,
    }
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in required.items()):
        raise ValueError("protocol authorization differs from frozen v3")
    for field in (
        "protocol_identity_sha256",
        "corpus_sha256",
        "code_sha",
        "embedding_profile_id",
        "qualification_evidence_sha256",
        "product_provenance_sha256",
        "product_provenance_archive_key",
        "product_provenance_archive_version_id",
        "authorized_by",
    ):
        if not str(payload.get(field) or ""):
            raise ValueError(f"protocol authorization has no {field}")
    for field in (
        "qualification_run_id",
        "product_run_id",
        "authorization_workflow_run_id",
        "authorization_workflow_run_attempt",
    ):
        _positive_int(payload.get(field), field)


def _validate_execution_payload(
    payload: dict[str, Any], *, sequence: int, protocol_sha256: str
) -> None:
    expected = {
        "schema_version": 1,
        "protocol_authorization_id": protocol_authorization_id(),
        "protocol_authorization_sha256": protocol_sha256,
        "execution_authorization_id": execution_authorization_id(sequence),
        "sequence": sequence,
    }
    if not isinstance(payload, dict) or any(payload.get(key) != value for key, value in expected.items()):
        raise ValueError("execution authorization identity mismatch")
    _positive_int(payload.get("authorization_workflow_run_id"), "workflow run id")
    _positive_int(payload.get("authorization_workflow_run_attempt"), "workflow run attempt")
    if not str(payload.get("authorized_by") or ""):
        raise ValueError("execution authorization has no owner identity")


def _validate_study_report(
    report: dict[str, Any], *, sequence: int, consumption: dict[str, Any]
) -> None:
    expected = {
        "schema_version": 1,
        "sequence": sequence,
        "execution_authorization_id": execution_authorization_id(sequence),
        "workflow_run_id": consumption["workflow_run_id"],
        "workflow_run_attempt": consumption["workflow_run_attempt"],
        "code_sha": consumption["code_sha"],
    }
    if not isinstance(report, dict) or any(report.get(key) != value for key, value in expected.items()):
        raise ValueError("study report does not match its consumed execution")
    if report.get("result") not in {"accepted", "not_demonstrated", "inconclusive"}:
        raise ValueError("study report has no terminal scientific result")
    if report.get("terminal_class") not in {
        "claim_authorized",
        "not_demonstrated",
        "scientific_terminal",
        "protocol_terminal",
        "infrastructure_outcome_bearing",
        "infrastructure_outcome_free",
    }:
        raise ValueError("study report has no valid terminal class")
    for field in ("reason_code", "terminal_reason", "protocol_identity_sha256"):
        if not str(report.get(field) or ""):
            raise ValueError(f"study report has no {field}")


def _positive_int(value: Any, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return parsed


def _require_sequence(sequence: int) -> None:
    if sequence not in {1, 2}:
        raise ValueError("learning execution sequence must be one or two")
