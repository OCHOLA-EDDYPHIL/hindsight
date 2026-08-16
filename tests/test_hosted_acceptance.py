"""Opt-in end-to-end acceptance against the deployed demo control plane."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib import error, request
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from hindsight.server_tenants import PUBLIC_DEMO_TENANT_ID

SOURCE_REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")
QUARANTINE_EVIDENCE_KEYS = (
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
)
requires_hosted_acceptance = pytest.mark.skipif(
    os.environ.get("RUN_HOSTED_ACCEPTANCE") != "1",
    reason="hosted acceptance is opt-in",
)


@pytest.fixture
def hosted_quarantine_records():
    import boto3

    from hindsight.aws import aws_client_config

    dynamodb = boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        config=aws_client_config(read_timeout=10),
    )
    table = dynamodb.Table(_required_env("HINDSIGHT_QUARANTINE_TABLE"))
    records: list[dict[str, object]] = []
    try:
        yield table, records
    finally:
        _cleanup_quarantine_records(table=table, records=records)


def _cleanup_quarantine_records(*, table, records: list[dict[str, object]]) -> None:
    from boto3.dynamodb.conditions import Attr

    for check in range(3):
        for record in records:
            stored = table.get_item(
                Key={"quarantine_id": record["quarantine_id"]},
                ConsistentRead=True,
            ).get("Item")
            if stored is None:
                continue
            if any(
                stored.get(key) != record[key]
                for key in ("quarantine_id", "source_arn", "source_message_id")
            ):
                pytest.fail(
                    f"synthetic quarantine cleanup identity conflicted: {record['quarantine_id']}"
                )
            captured = {
                key: stored.get(key) for key in ("raw_body_sha256", "record_sha256", "status")
            }
            if any(not isinstance(value, str) or not value for value in captured.values()):
                pytest.fail(
                    f"synthetic quarantine cleanup binding was incomplete: {record['quarantine_id']}"
                )
            condition = (
                Attr("quarantine_id").eq(record["quarantine_id"])
                & Attr("source_arn").eq(record["source_arn"])
                & Attr("source_message_id").eq(record["source_message_id"])
                & Attr("raw_body_sha256").eq(captured["raw_body_sha256"])
                & Attr("record_sha256").eq(captured["record_sha256"])
                & Attr("status").eq(captured["status"])
            )
            if "run_id" in stored:
                condition &= Attr("run_id").eq(stored["run_id"])
            else:
                condition &= Attr("run_id").not_exists()
            table.delete_item(
                Key={"quarantine_id": record["quarantine_id"]},
                ConditionExpression=condition,
            )
        if check < 2 and records:
            time.sleep(1)
    for record in records:
        remaining = table.get_item(
            Key={"quarantine_id": record["quarantine_id"]},
            ConsistentRead=True,
        ).get("Item")
        if remaining is not None:
            pytest.fail(f"synthetic quarantine cleanup failed: {record['quarantine_id']}")


@requires_hosted_acceptance
def test_resolved_transition_reaches_managed_changefeed_worker_and_cited_lesson():
    from hindsight.db import connect
    from hindsight.embeddings import embedding_provider_from_env
    from hindsight.gemini import gemini_pool_from_env
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.runs import create_incident
    from hindsight.runtime import runtime_settings

    api_url = _required_env("HOSTED_API_URL").rstrip("/")
    operator_token = _required_env("HINDSIGHT_PRODUCT_ACCESS_TOKEN")
    settings = runtime_settings(use_cache=False)
    pool = gemini_pool_from_env(settings.provider_env)
    embeddings = embedding_provider_from_env(settings.provider_env, gemini_pool=pool)
    assert embeddings.provider_name == "gemini"
    assert embeddings.capability == "semantic"

    token = uuid4().hex
    namespace = f"live-managed-consolidation:{token}"
    slug = f"live-managed-consolidation:{token}"
    incident = create_incident(
        slug=slug,
        title="Checkout stalls under downstream retry amplification",
        severity="sev2",
        summary="Purchases stall in waves when downstream work multiplies.",
        db_url=settings.database_url,
    )
    with MemoryStore(url=settings.database_url, embedding_provider=embeddings) as store:
        source = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content=(
                "Checkout latency rose as processor timeouts amplified retry fanout "
                "and queue depth."
            ),
            provenance=Provenance(
                "live.hosted_acceptance",
                f"incident:{incident['id']}:summary",
                "Verified source evidence for managed consolidation",
            ),
            content_schema="incident_summary.v1",
            structured_payload={"incident_id": str(incident["id"])},
        )
    with connect(settings.database_url, application_name="hindsight-hosted-acceptance") as conn:
        conn.execute(
            """
                INSERT INTO incident_semantic_memories (incident_id, memory_id, relationship)
                VALUES (%s, %s, 'summary')
            """,
            (incident["id"], source["id"]),
        )
        conn.commit()

    resolution = _post_json(
        f"{api_url}/v2/incidents/{slug}/resolution",
        token=operator_token,
        payload={
            "root_cause": "Retry amplification overloaded an unhealthy payment processor.",
            "action": "Inspect processor health and throttle retry fanout.",
            "observation": "Timeout rate and queue depth recovered after fanout was throttled.",
            "recovered": True,
        },
    )
    event_id = str(resolution["event"]["id"])

    job = _wait_for_managed_consolidation_job(
        incident_id=incident["id"],
        event_id=event_id,
        database_url=settings.database_url,
        timeout=300,
    )
    assert str(job[1]) == event_id
    assert job[2]
    assert job[4]

    with connect(settings.database_url, application_name="hindsight-hosted-acceptance") as conn:
        lesson = conn.execute(
            """
                SELECT namespace, writer, source_ref, content_schema, structured_payload,
                       trust_status, t_invalid
                FROM semantic_memories WHERE id = %s
            """,
            (job[4],),
        ).fetchone()
        source_read = conn.execute(
            """
                SELECT count(*) FROM memory_reads
                WHERE decision_id = %s AND semantic_memory_id = %s
            """,
            (job[2], source["id"]),
        ).fetchone()[0]
        lesson_link = conn.execute(
            """
                SELECT relationship FROM incident_semantic_memories
                WHERE incident_id = %s AND memory_id = %s
            """,
            (incident["id"], job[4]),
        ).fetchone()

    assert lesson is not None
    assert lesson[0] == namespace
    assert lesson[1] == "consolidation.worker"
    assert lesson[2] == f"incident_event:{event_id}"
    assert lesson[3] == "procedural_lesson.v1"
    claims = dict(lesson[4])["claims"]
    assert claims and all(claim["citations"] for claim in claims)
    assert lesson[5] == "review_required"
    assert lesson[6] is None
    assert source_read == 1
    assert lesson_link == ("lesson",)

    excluded_decision = f"live-managed-candidate-exclusion:{token}"
    with MemoryStore(url=settings.database_url, embedding_provider=embeddings) as store:
        excluded = store.retrieve_semantic(
            namespace=namespace,
            query=(
                "What reusable response should we take when a remote payment processor "
                "stalls and repeated attempts multiply the queue?"
            ),
            decision_id=excluded_decision,
            reader="live.hosted_acceptance",
            purpose="Verify the generated candidate is excluded before operator review",
            policy="semantic_strict",
            limit=5,
        )
        store.seal_decision(decision_id=excluded_decision)
    assert str(job[4]) not in {str(hit["id"]) for hit in excluded.hits}

    preview = _post_json(
        f"{api_url}/v2/memory/consolidation-candidates/{job[0]}/review-preview",
        token=operator_token,
        payload={
            "action": "approve",
            "reason": "Citations, entailment, and operational safety reviewed",
        },
    )
    accepted = _post_json(
        f"{api_url}/v2/memory/operations",
        token=operator_token,
        payload={"preview_id": preview["id"], "fingerprint": preview["fingerprint"]},
        extra_headers={"Idempotency-Key": uuid4().hex},
    )
    operation_deadline = time.monotonic() + 180
    operation_status = None
    approved_memory_id = None
    while time.monotonic() < operation_deadline:
        with connect(
            settings.database_url,
            application_name="hindsight-hosted-acceptance",
        ) as conn:
            operation_status = conn.execute(
                "SELECT status FROM memory_operations WHERE id = %s",
                (accepted["operation_id"],),
            ).fetchone()[0]
            approved_memory_id = conn.execute(
                "SELECT approved_memory_id FROM consolidation_jobs WHERE id = %s",
                (job[0],),
            ).fetchone()[0]
        if operation_status == "completed":
            break
        if operation_status in {"conflict", "failed"}:
            pytest.fail(f"candidate approval ended in {operation_status}")
        time.sleep(2)
    assert operation_status == "completed"
    assert approved_memory_id is not None

    retrieval_decision = f"live-managed-lesson-retrieval:{token}"
    with MemoryStore(url=settings.database_url, embedding_provider=embeddings) as store:
        retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=(
                "What reusable response should we take when a remote payment processor "
                "stalls and repeated attempts multiply the queue?"
            ),
            decision_id=retrieval_decision,
            reader="live.hosted_acceptance",
            purpose="Verify the operator-approved successor is usable by semantic recall",
            policy="semantic_strict",
            limit=5,
        )
        store.seal_decision(decision_id=retrieval_decision)
    assert retrieval.selected_strategy == "semantic_vector"
    assert str(approved_memory_id) in {str(hit["id"]) for hit in retrieval.hits}

    rejection_token = uuid4().hex
    rejection_namespace = f"live-managed-consolidation:{rejection_token}"
    rejection_slug = f"live-managed-consolidation:{rejection_token}"
    rejection_incident = create_incident(
        slug=rejection_slug,
        title="Checkout stalls under downstream retry amplification",
        severity="sev2",
        summary="Purchases stall in waves when downstream work multiplies.",
        db_url=settings.database_url,
    )
    with MemoryStore(url=settings.database_url, embedding_provider=embeddings) as store:
        rejection_source = store.remember(
            memory_kind="semantic",
            namespace=rejection_namespace,
            content=(
                "Checkout latency rose as processor timeouts amplified retry fanout "
                "and queue depth."
            ),
            provenance=Provenance(
                "live.hosted_acceptance",
                f"incident:{rejection_incident['id']}:summary",
                "Verified source evidence for managed consolidation rejection",
            ),
            content_schema="incident_summary.v1",
            structured_payload={"incident_id": str(rejection_incident["id"])},
        )
    with connect(settings.database_url, application_name="hindsight-hosted-acceptance") as conn:
        conn.execute(
            """
                INSERT INTO incident_semantic_memories (incident_id, memory_id, relationship)
                VALUES (%s, %s, 'summary')
            """,
            (rejection_incident["id"], rejection_source["id"]),
        )
        conn.commit()

    rejection_resolution = _post_json(
        f"{api_url}/v2/incidents/{rejection_slug}/resolution",
        token=operator_token,
        payload={
            "root_cause": "Retry amplification overloaded an unhealthy payment processor.",
            "action": "Inspect processor health and throttle retry fanout.",
            "observation": "Timeout rate and queue depth recovered after fanout was throttled.",
            "recovered": True,
        },
    )
    rejection_event_id = str(rejection_resolution["event"]["id"])

    rejection_job = _wait_for_managed_consolidation_job(
        incident_id=rejection_incident["id"],
        event_id=rejection_event_id,
        database_url=settings.database_url,
        timeout=300,
    )
    assert str(rejection_job[1]) == rejection_event_id
    assert rejection_job[2]
    assert rejection_job[4]
    assert rejection_namespace != namespace
    assert rejection_job[0] != job[0]
    assert rejection_job[4] != job[4]

    rejection_reason = "The proposed response is too broad for safe operational reuse"
    rejection_preview = _post_json(
        f"{api_url}/v2/memory/consolidation-candidates/{rejection_job[0]}/review-preview",
        token=operator_token,
        payload={"action": "reject", "reason": rejection_reason},
    )
    rejected = _post_json(
        f"{api_url}/v2/memory/operations",
        token=operator_token,
        payload={
            "preview_id": rejection_preview["id"],
            "fingerprint": rejection_preview["fingerprint"],
        },
        extra_headers={"Idempotency-Key": uuid4().hex},
    )
    rejection_operation_deadline = time.monotonic() + 180
    rejection_operation_status = None
    while time.monotonic() < rejection_operation_deadline:
        with connect(
            settings.database_url,
            application_name="hindsight-hosted-acceptance",
        ) as conn:
            rejection_operation_status = conn.execute(
                "SELECT status FROM memory_operations WHERE id = %s",
                (rejected["operation_id"],),
            ).fetchone()[0]
        if rejection_operation_status == "completed":
            break
        if rejection_operation_status in {"conflict", "failed"}:
            pytest.fail(f"candidate rejection ended in {rejection_operation_status}")
        time.sleep(2)
    assert rejection_operation_status == "completed"

    with connect(settings.database_url, application_name="hindsight-hosted-acceptance") as conn:
        rejection_review = conn.execute(
            """
                SELECT review_status, review_reason, review_operation_id::text,
                       approved_memory_id
                FROM consolidation_jobs WHERE id = %s
            """,
            (rejection_job[0],),
        ).fetchone()
        rejected_memory = conn.execute(
            """
                SELECT trust_status, t_invalid
                FROM semantic_memories WHERE id = %s
            """,
            (rejection_job[4],),
        ).fetchone()
    assert rejection_review == (
        "rejected",
        rejection_reason,
        rejected["operation_id"],
        None,
    )
    assert rejected_memory == ("review_required", None)

    rejected_decision = f"live-managed-rejected-exclusion:{rejection_token}"
    with MemoryStore(url=settings.database_url, embedding_provider=embeddings) as store:
        rejected_retrieval = store.retrieve_semantic(
            namespace=rejection_namespace,
            query=(
                "What reusable response should we take when a remote payment processor "
                "stalls and repeated attempts multiply the queue?"
            ),
            decision_id=rejected_decision,
            reader="live.hosted_acceptance",
            purpose="Verify the operator-rejected candidate remains excluded",
            policy="semantic_strict",
            limit=5,
        )
        store.seal_decision(decision_id=rejected_decision)
    assert str(rejection_job[4]) not in {str(hit["id"]) for hit in rejected_retrieval.hits}


@requires_hosted_acceptance
def test_scheduled_dispatch_reclaims_and_source_terminalizes_with_quarantine(
    hosted_quarantine_records,
):
    import boto3

    from hindsight.aws import aws_client_config
    from hindsight.db import connect
    from hindsight.quarantine import raw_body_digest, stable_quarantine_id
    from hindsight.run_dispatch import _complete_run_dispatch
    from hindsight.runs import (
        claim_run_attempt,
        create_incident,
        create_run,
        get_run,
        prepare_approval,
    )
    from hindsight.runtime import runtime_database_url

    database_url = runtime_database_url()
    token = _acceptance_token("worker")
    lease_seconds = _positive_int_env("HINDSIGHT_ACCEPTANCE_RUN_ATTEMPT_LEASE_SECONDS")
    visibility_seconds = _positive_int_env("HINDSIGHT_ACCEPTANCE_QUEUE_VISIBILITY_SECONDS")
    max_attempts = _positive_int_env("HINDSIGHT_ACCEPTANCE_RUN_MAX_ATTEMPTS")
    scheduler_seconds = _positive_int_env("HINDSIGHT_ACCEPTANCE_SCHEDULER_SECONDS")
    run_queue_url = _required_env("HINDSIGHT_ACCEPTANCE_RUN_QUEUE_URL")
    run_queue_arn = _required_env("HINDSIGHT_ACCEPTANCE_RUN_QUEUE_ARN")
    assert max_attempts == 3
    quarantine_table, quarantine_records = hosted_quarantine_records
    sqs = boto3.client(
        "sqs",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    )

    pending_slug = f"hosted-pending:{token}"
    create_incident(
        slug=pending_slug,
        title="Scheduled queue and approval validation",
        severity="sev3",
        summary="Verify the hosted dispatcher and approval resume path.",
        db_url=database_url,
    )
    pending, _ = create_run(
        incident_slug=pending_slug,
        namespace=f"hosted-pending:{token}",
        user_input="Verify scheduled recovery of a deliberately pending command",
        db_url=database_url,
    )
    awaiting = _wait_for_run_status(
        str(pending["id"]),
        expected={"awaiting_approval"},
        database_url=database_url,
        timeout=scheduler_seconds * 2 + lease_seconds + 180,
    )
    assert awaiting["worker_attempt_count"] == 1
    action_trace = awaiting["action_trace"]
    prepare_approval(
        run_id=pending["id"],
        approved=True,
        recommendation_id=action_trace["recommendation"]["id"],
        selection_fingerprint=action_trace["selection"]["fingerprint"],
        db_url=database_url,
    )
    _wait_for_run_status(
        str(pending["id"]),
        expected={"completed"},
        database_url=database_url,
        timeout=scheduler_seconds * 2 + lease_seconds + 180,
    )

    reclaimed_slug = f"hosted-reclaim:{token}"
    create_incident(
        slug=reclaimed_slug,
        title="Scheduled queue and approval validation",
        severity="sev3",
        summary="Verify the hosted dispatcher and approval resume path.",
        db_url=database_url,
    )
    reclaimed, _ = create_run(
        incident_slug=reclaimed_slug,
        namespace=f"hosted-reclaim:{token}",
        user_input="Verify scheduled recovery of a deliberately pending command",
        dispatch_available_at=datetime.now(UTC) + timedelta(seconds=5),
        db_url=database_url,
    )
    first = claim_run_attempt(
        run_id=reclaimed["id"],
        command="start",
        command_generation=0,
        lease_ttl=timedelta(seconds=1),
        max_attempts=max_attempts,
        db_url=database_url,
    )
    assert first.outcome == "claimed"
    time.sleep(1.1)
    recovered = _wait_for_run_status(
        str(reclaimed["id"]),
        expected={"awaiting_approval"},
        database_url=database_url,
        timeout=scheduler_seconds * 2 + lease_seconds + 180,
    )
    assert recovered["worker_attempt_count"] == 2
    assert any(event["phase"] == "recovery" for event in recovered["events"])

    exhausted_slug = f"hosted-quarantine:{token}"
    create_incident(
        slug=exhausted_slug,
        title="Source-delivery terminalization validation",
        severity="sev3",
        summary="Verify exhausted attempts finalize before source acknowledgement.",
        db_url=database_url,
    )
    exhausted, _ = create_run(
        incident_slug=exhausted_slug,
        namespace=f"hosted-quarantine:{token}",
        user_input="Verify exhausted work is terminalized on the final source delivery",
        dispatch_available_at=(datetime.now(UTC) + timedelta(days=3650)),
        db_url=database_url,
    )
    dispatch_attempt_id = str(uuid4())
    dispatch_lease_owner = str(uuid4())
    with connect(database_url, application_name="hindsight-hosted-acceptance") as conn:
        with conn.transaction():
            dispatch = conn.execute(
                """
                    SELECT id, payload, attempt_count
                    FROM agent_run_dispatches
                    WHERE run_id = %s AND command = 'start'
                    FOR UPDATE
                """,
                (exhausted["id"],),
            ).fetchone()
            assert dispatch is not None
            dispatch_id = str(dispatch[0])
            dispatch_sequence = int(dispatch[2]) + 1
            leased = conn.execute(
                """
                    UPDATE agent_run_dispatches
                    SET status = 'leased', attempt_count = %s,
                        lease_owner = %s,
                        lease_expires_at = now() + INTERVAL '3650 days',
                        transport_message_id = NULL, dispatched_at = NULL,
                        acknowledged_attempt_id = NULL, acknowledged_at = NULL,
                        available_at = now() + INTERVAL '3650 days',
                        updated_at = now(), last_error = NULL
                    WHERE id = %s AND status = 'pending'
                    RETURNING id
                """,
                (dispatch_sequence, dispatch_lease_owner, dispatch_id),
            ).fetchone()
            assert leased == (dispatch[0],)
            conn.execute(
                """
                    INSERT INTO agent_run_dispatch_attempts (
                        id, dispatch_id, sequence, lease_owner, lease_expires_at
                    )
                    VALUES (%s, %s, %s, %s, now() + INTERVAL '3650 days')
                """,
                (
                    dispatch_attempt_id,
                    dispatch_id,
                    dispatch_sequence,
                    dispatch_lease_owner,
                ),
            )
            exhausted_message = {
                **dict(dispatch[1]),
                "tenant_id": PUBLIC_DEMO_TENANT_ID,
                "dispatch_id": dispatch_id,
                "dispatch_attempt_id": dispatch_attempt_id,
                "dispatch_sequence": dispatch_sequence,
            }
    for expected_attempt in range(1, max_attempts + 1):
        claim = claim_run_attempt(
            run_id=exhausted["id"],
            command="start",
            command_generation=0,
            lease_ttl=timedelta(seconds=1),
            max_attempts=max_attempts,
            db_url=database_url,
        )
        assert claim.outcome == "claimed"
        assert claim.run["worker_attempt_count"] == expected_attempt
        time.sleep(1.1)
    sent = sqs.send_message(
        QueueUrl=run_queue_url,
        MessageBody=json.dumps(exhausted_message, sort_keys=True, separators=(",", ":")),
    )
    exhausted_message_id = str(sent["MessageId"])
    exhausted_quarantine_id = stable_quarantine_id(
        source_arn=run_queue_arn,
        source_message_id=exhausted_message_id,
    )
    exhausted_cleanup = {
        "quarantine_id": exhausted_quarantine_id,
        "source_arn": run_queue_arn,
        "source_message_id": exhausted_message_id,
    }
    quarantine_records.append(exhausted_cleanup)
    assert _complete_run_dispatch(
        dispatch_id=dispatch_id,
        dispatch_attempt_id=dispatch_attempt_id,
        lease_owner=dispatch_lease_owner,
        message_id=exhausted_message_id,
        db_url=database_url,
    )
    failed = _wait_for_run_status(
        str(exhausted["id"]),
        expected={"failed"},
        database_url=database_url,
        timeout=visibility_seconds * 2 + 120,
    )
    exhausted_quarantine = _wait_for_quarantine_id(
        table=quarantine_table,
        quarantine_id=exhausted_quarantine_id,
        timeout=visibility_seconds * 2 + 120,
    )
    exhausted_cleanup.clear()
    exhausted_cleanup.update(exhausted_quarantine)
    assert failed["failure_code"] == "RunAttemptsExhausted"
    assert [event["status"] for event in failed["events"]].count("failed") == 1
    assert exhausted_quarantine["status"] == "quarantined"
    assert exhausted_quarantine["reason_code"] == "run_attempts_exhausted"
    assert exhausted_quarantine["work_kind"] == "run"
    assert exhausted_quarantine["command"] == "start"
    assert exhausted_quarantine["command_generation"] == 0
    assert exhausted_quarantine["source_arn"] == run_queue_arn
    assert exhausted_quarantine["source_message_id"] == exhausted_message_id
    assert exhausted_quarantine["tenant_id"] == PUBLIC_DEMO_TENANT_ID
    assert exhausted_quarantine["run_id"] == str(exhausted["id"])
    assert re.fullmatch(r"[0-9a-f]{64}", str(exhausted_quarantine["raw_body_sha256"]))
    with connect(database_url, application_name="hindsight-hosted-acceptance") as conn:
        decision = conn.execute(
            "SELECT status, sealed_at IS NOT NULL FROM memory_decisions WHERE id = %s",
            (failed["decision_id"],),
        ).fetchone()
    assert decision == ("failed", True)

    missing_run_id = str(uuid4())
    malformed_body = json.dumps(
        {"command": "start", "run_id": missing_run_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    sent = sqs.send_message(QueueUrl=run_queue_url, MessageBody=malformed_body)
    malformed_message_id = str(sent["MessageId"])
    malformed_quarantine_id = stable_quarantine_id(
        source_arn=run_queue_arn,
        source_message_id=malformed_message_id,
    )
    malformed_cleanup = {
        "quarantine_id": malformed_quarantine_id,
        "source_arn": run_queue_arn,
        "source_message_id": malformed_message_id,
    }
    quarantine_records.append(malformed_cleanup)
    malformed_quarantine = _wait_for_quarantine_id(
        table=quarantine_table,
        quarantine_id=malformed_quarantine_id,
        timeout=visibility_seconds * 2 + 120,
    )
    malformed_cleanup.clear()
    malformed_cleanup.update(malformed_quarantine)
    assert malformed_quarantine["status"] == "quarantined"
    assert malformed_quarantine["source_arn"] == run_queue_arn
    assert malformed_quarantine["source_message_id"] == malformed_message_id
    assert malformed_quarantine["raw_body_sha256"] == raw_body_digest(malformed_body)
    assert malformed_quarantine["reason_code"] == "invalid_envelope"
    assert malformed_quarantine["work_kind"] == "unknown"
    assert malformed_quarantine["command"] == "start"
    assert "run_id" not in malformed_quarantine
    assert "operation_id" not in malformed_quarantine
    assert "raw_body" not in malformed_quarantine
    assert missing_run_id not in repr(malformed_quarantine)
    assert get_run(run_id=missing_run_id, db_url=database_url) is None
    _write_quarantine_evidence(quarantine_records)
    _write_redrive_handoff(exhausted_quarantine)
    quarantine_records.remove(exhausted_cleanup)


def _write_quarantine_evidence(records: list[dict[str, object]]) -> None:
    from hindsight.quarantine import validate_stored_quarantine_item

    source_revision = _required_env("HINDSIGHT_ACCEPTANCE_SOURCE_REVISION")
    if SOURCE_REVISION_PATTERN.fullmatch(source_revision) is None:
        raise ValueError("HINDSIGHT_ACCEPTANCE_SOURCE_REVISION must be a full lowercase SHA")
    artifact_dir = Path(_required_env("HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR")).resolve()
    evidence_path = Path(_required_env("HINDSIGHT_QUARANTINE_EVIDENCE_PATH")).resolve()
    if evidence_path.parent != artifact_dir or evidence_path.name != "quarantine-evidence.json":
        raise ValueError("quarantine evidence path must be the configured acceptance artifact")
    redacted_records = []
    for record in records:
        validated = validate_stored_quarantine_item(record)
        redacted_records.append(
            {key: validated[key] for key in QUARANTINE_EVIDENCE_KEYS if key in validated}
        )
    redacted_records.sort(key=lambda record: str(record["quarantine_id"]))
    payload = {
        "schema_version": 1,
        "source_revision": source_revision,
        "records": redacted_records,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    evidence = {**payload, "payload_sha256": hashlib.sha256(canonical).hexdigest()}
    evidence_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_redrive_handoff(record: dict[str, object]) -> None:
    from scripts.exercise_quarantine_redrive import (
        HANDOFF_RECORD_KEYS,
        load_redrive_handoff,
    )
    from hindsight.quarantine import validate_stored_quarantine_item

    source_revision = _required_env("HINDSIGHT_ACCEPTANCE_SOURCE_REVISION")
    if SOURCE_REVISION_PATTERN.fullmatch(source_revision) is None:
        raise ValueError("HINDSIGHT_ACCEPTANCE_SOURCE_REVISION must be a full lowercase SHA")
    artifact_dir = Path(_required_env("HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR")).resolve()
    handoff_path = Path(_required_env("HINDSIGHT_REDRIVE_HANDOFF_PATH")).resolve()
    if handoff_path.parent != artifact_dir or handoff_path.name != "redrive-handoff.json":
        raise ValueError("redrive handoff path must be the configured acceptance artifact")
    validated = validate_stored_quarantine_item(record)
    if (
        validated.get("status") != "quarantined"
        or validated.get("reason_code") != "run_attempts_exhausted"
        or validated.get("work_kind") != "run"
    ):
        raise ValueError("redrive handoff must bind one exhausted quarantined run")
    payload = {
        "schema_version": 1,
        "source_revision": source_revision,
        "record": {key: validated[key] for key in sorted(HANDOFF_RECORD_KEYS)},
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    evidence = {**payload, "payload_sha256": hashlib.sha256(canonical).hexdigest()}
    handoff_path.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    loaded = load_redrive_handoff(handoff_path)
    if loaded.source_revision != source_revision or loaded.record != payload["record"]:
        raise ValueError("redrive handoff did not round-trip through its consumer contract")


def test_quarantine_evidence_is_revision_bound_redacted_and_digestible(
    monkeypatch,
    tmp_path,
):
    from scripts.exercise_quarantine_redrive import HANDOFF_RECORD_KEYS
    from hindsight.quarantine import persist_quarantine_record
    from tests.quarantine_fakes import InMemoryQuarantineTable

    raw_body = '{"command":"start","prompt":"must-not-leak"}'
    record = persist_quarantine_record(
        table=InMemoryQuarantineTable(),
        source_arn="arn:aws:sqs:us-east-1:123456789012:hindsight-runs",
        source_message_id="11111111-1111-4111-8111-111111111111",
        raw_body=raw_body,
        reason_code="run_attempts_exhausted",
        work_kind="run",
        command="start",
        receive_count=4,
        tenant_id="00000000-0000-0000-0000-000000000001",
        run_id="22222222-2222-4222-8222-222222222222",
        command_generation=0,
    ).item
    evidence_path = tmp_path / "quarantine-evidence.json"
    handoff_path = tmp_path / "redrive-handoff.json"
    monkeypatch.setenv("HINDSIGHT_ACCEPTANCE_SOURCE_REVISION", "a" * 40)
    monkeypatch.setenv("HINDSIGHT_ACCEPTANCE_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("HINDSIGHT_QUARANTINE_EVIDENCE_PATH", str(evidence_path))
    monkeypatch.setenv("HINDSIGHT_REDRIVE_HANDOFF_PATH", str(handoff_path))

    _write_quarantine_evidence([record])
    _write_redrive_handoff(record)

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload_digest = evidence.pop("payload_sha256")
    canonical = json.dumps(
        evidence,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert payload_digest == hashlib.sha256(canonical).hexdigest()
    assert evidence["source_revision"] == "a" * 40
    assert evidence["records"] == [
        {key: record[key] for key in QUARANTINE_EVIDENCE_KEYS if key in record}
    ]
    assert raw_body not in evidence_path.read_text(encoding="utf-8")
    assert "must-not-leak" not in evidence_path.read_text(encoding="utf-8")
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    handoff_digest = handoff.pop("payload_sha256")
    canonical_handoff = json.dumps(
        handoff,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert handoff_digest == hashlib.sha256(canonical_handoff).hexdigest()
    assert handoff == {
        "schema_version": 1,
        "source_revision": "a" * 40,
        "record": {key: record[key] for key in HANDOFF_RECORD_KEYS},
    }
    assert raw_body not in handoff_path.read_text(encoding="utf-8")
    assert "must-not-leak" not in handoff_path.read_text(encoding="utf-8")


def test_synthetic_quarantine_cleanup_is_bound_to_each_validated_record(monkeypatch):
    from boto3.dynamodb.conditions import ConditionExpressionBuilder

    from hindsight.quarantine import persist_quarantine_record
    from tests.quarantine_fakes import InMemoryQuarantineTable

    source_arn = "arn:aws:sqs:us-east-1:123456789012:hindsight-runs"
    table = InMemoryQuarantineTable()
    exhausted = persist_quarantine_record(
        table=table,
        source_arn=source_arn,
        source_message_id="11111111-1111-4111-8111-111111111111",
        raw_body='{"command":"start"}',
        reason_code="run_attempts_exhausted",
        work_kind="run",
        command="start",
        receive_count=4,
        tenant_id="00000000-0000-0000-0000-000000000001",
        run_id="22222222-2222-4222-8222-222222222222",
        command_generation=0,
    ).item
    malformed = persist_quarantine_record(
        table=table,
        source_arn=source_arn,
        source_message_id="33333333-3333-4333-8333-333333333333",
        raw_body='{"command":"start"}',
        reason_code="invalid_envelope",
        work_kind="unknown",
        command="start",
        receive_count=1,
    ).item
    delete_calls = []

    def delete_item(**kwargs):
        delete_calls.append(kwargs)
        table.items.pop(str(kwargs["Key"]["quarantine_id"]))

    table.delete_item = delete_item
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    cleanup_bindings = [
        {key: record[key] for key in ("quarantine_id", "source_arn", "source_message_id")}
        for record in (exhausted, malformed)
    ]
    _cleanup_quarantine_records(table=table, records=cleanup_bindings)

    assert table.items == {}
    assert [call["Key"] for call in delete_calls] == [
        {"quarantine_id": exhausted["quarantine_id"]},
        {"quarantine_id": malformed["quarantine_id"]},
    ]
    required_attributes = {
        "quarantine_id",
        "source_arn",
        "source_message_id",
        "raw_body_sha256",
        "record_sha256",
        "status",
        "run_id",
    }
    built = [
        ConditionExpressionBuilder().build_expression(call["ConditionExpression"])
        for call in delete_calls
    ]
    for expression in built:
        assert required_attributes <= set(expression.attribute_name_placeholders.values())
    assert "attribute_not_exists" not in built[0].condition_expression
    assert "attribute_not_exists" in built[1].condition_expression


def _wait_for_quarantine_id(
    *,
    table,
    quarantine_id: str,
    timeout: int,
) -> dict[str, object]:
    from hindsight.quarantine import validate_stored_quarantine_item

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = table.get_item(
            Key={"quarantine_id": quarantine_id},
            ConsistentRead=True,
        ).get("Item")
        if item is not None:
            return validate_stored_quarantine_item(item)
        time.sleep(2)
    pytest.fail(f"quarantine record was not created before timeout: {quarantine_id}")


def _wait_for_run_status(
    run_id: str,
    *,
    expected: set[str],
    database_url: str,
    timeout: int,
) -> dict[str, object]:
    from hindsight.runs import get_run

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = get_run(run_id=run_id, db_url=database_url)
        if last is not None and last["status"] in expected:
            return last
        if last is not None and last["status"] in {"completed", "rejected", "failed"}:
            pytest.fail(f"run reached unexpected terminal status: {last['status']}")
        time.sleep(2)
    pytest.fail(f"run did not reach {sorted(expected)} before timeout: {last}")


def _wait_for_managed_consolidation_job(
    *,
    incident_id,
    event_id: str,
    database_url: str,
    timeout: int,
):
    from hindsight.db import connect

    deadline = time.monotonic() + timeout
    job = None
    while time.monotonic() < deadline:
        with connect(
            database_url,
            application_name="hindsight-hosted-acceptance",
        ) as conn:
            job = conn.execute(
                """
                    SELECT id, source_event_id, decision_id, status, lesson_memory_id,
                           error_code, error_detail
                    FROM consolidation_jobs
                    WHERE incident_id = %s AND source_event_id = %s
                """,
                (incident_id, event_id),
            ).fetchone()
        if job is not None and job[3] == "completed":
            return job
        if job is not None and job[3] in {"failed", "not_eligible"}:
            pytest.fail(
                "managed consolidation terminated without a lesson: "
                f"status={job[3]} code={job[5]} detail={job[6]}"
            )
        time.sleep(2)
    pytest.fail(f"managed consolidation did not complete before timeout: {job}")


def _acceptance_token(label: str) -> str:
    phase_id = os.environ.get("HINDSIGHT_ACCEPTANCE_PHASE_ID") or str(uuid4())
    return f"{label}:{phase_id}:{uuid4()}"


def _positive_int_env(name: str) -> int:
    value = int(_required_env(name))
    assert value > 0
    return value


@requires_hosted_acceptance
def test_websocket_requires_resubscribe_after_reconnect_and_honors_unsubscribe():
    from websockets.asyncio.client import connect

    api_url = _required_env("HOSTED_API_URL").rstrip("/")
    websocket_url = _required_env("HINDSIGHT_WEBSOCKET_URL")
    changefeed_token = _required_env("HINDSIGHT_CHANGEFEED_AUTH_TOKEN")
    namespace = f"live-websocket-lifecycle:{uuid4().hex}"

    async def scenario() -> None:
        first_url = await asyncio.to_thread(
            _public_websocket_url,
            api_url=api_url,
            websocket_url=websocket_url,
        )
        first = await connect(first_url, open_timeout=15, close_timeout=15)
        await first.send(json.dumps({"type": "subscribe", "namespace": namespace, "run_id": None}))
        await _wait_for_websocket_delivery(
            first,
            api_url=api_url,
            token=changefeed_token,
            namespace=namespace,
        )
        await _wait_for_expired_changefeed_lease_takeover(
            first,
            api_url=api_url,
            token=changefeed_token,
            namespace=namespace,
        )
        await first.close(code=1000)
        await first.wait_closed()
        assert first.close_code == 1000

        second_url = await asyncio.to_thread(
            _public_websocket_url,
            api_url=api_url,
            websocket_url=websocket_url,
        )
        async with connect(second_url, open_timeout=15, close_timeout=15) as second:
            disconnected = await asyncio.to_thread(
                _inject_changefeed_event,
                api_url=api_url,
                token=changefeed_token,
                namespace=namespace,
            )
            assert disconnected["delivered"] == 0
            await _assert_no_websocket_message(second)

            await second.send(
                json.dumps({"type": "subscribe", "namespace": namespace, "run_id": None})
            )
            await _wait_for_websocket_delivery(
                second,
                api_url=api_url,
                token=changefeed_token,
                namespace=namespace,
            )

            await second.send(json.dumps({"type": "unsubscribe"}))
            await _wait_for_websocket_silence(
                second,
                api_url=api_url,
                token=changefeed_token,
                namespace=namespace,
            )

    asyncio.run(scenario())


async def _wait_for_websocket_delivery(socket, *, api_url: str, token: str, namespace: str) -> None:
    for _ in range(20):
        result = await asyncio.to_thread(
            _inject_changefeed_event,
            api_url=api_url,
            token=token,
            namespace=namespace,
        )
        if result["delivered"] == 1:
            message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
            assert message["type"] == "run"
            assert message["version"] == 2
            assert message["event_id"] == result["event_id"]
            assert message["cursor"] == {
                "hlc": result["hlc"],
                "event_id": result["event_id"],
            }
            assert message["tenant_id"] == PUBLIC_DEMO_TENANT_ID
            assert message["topic_keys"] == [
                f"tenant:{PUBLIC_DEMO_TENANT_ID}:namespace:{namespace}"
            ]
            assert message["data"]["reference"]["run_id"] == result["event_id"]
            duplicate = await asyncio.to_thread(
                _inject_changefeed_event,
                api_url=api_url,
                token=token,
                namespace=namespace,
                event_id=str(result["event_id"]),
                hlc=str(result["hlc"]),
                expected_accepted=0,
            )
            assert duplicate["duplicates_ignored"] == 1
            assert duplicate["delivered"] == 0
            await _assert_no_websocket_message(socket)
            return
        assert result["delivered"] == 0
        await asyncio.sleep(0.5)
    pytest.fail("WebSocket subscription did not become active")


async def _wait_for_websocket_silence(socket, *, api_url: str, token: str, namespace: str) -> None:
    for _ in range(20):
        result = await asyncio.to_thread(
            _inject_changefeed_event,
            api_url=api_url,
            token=token,
            namespace=namespace,
        )
        if result["delivered"] == 0:
            break
        assert result["delivered"] == 1
        message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        assert message["data"]["reference"]["run_id"] == result["event_id"]
        await asyncio.sleep(0.5)
    else:
        pytest.fail("WebSocket unsubscribe did not become active")

    final = await asyncio.to_thread(
        _inject_changefeed_event,
        api_url=api_url,
        token=token,
        namespace=namespace,
    )
    assert final["delivered"] == 0
    await _assert_no_websocket_message(socket)


async def _assert_no_websocket_message(socket) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(socket.recv(), timeout=2)


async def _wait_for_expired_changefeed_lease_takeover(
    socket, *, api_url: str, token: str, namespace: str
) -> None:
    event_id = str(uuid4())
    hlc = f"{time.time_ns()}.0000000000"
    lease_expires_at = await asyncio.to_thread(
        _seed_processing_changefeed_lease,
        event_id=event_id,
        lease_seconds=3,
    )
    status, busy = await asyncio.to_thread(
        _post_changefeed_event,
        api_url=api_url,
        token=token,
        namespace=namespace,
        event_id=event_id,
        hlc=hlc,
    )
    assert status == 503
    assert busy["retryable"] is True

    await asyncio.sleep(max(0, lease_expires_at - int(time.time()) + 1))
    recovered = await asyncio.to_thread(
        _inject_changefeed_event,
        api_url=api_url,
        token=token,
        namespace=namespace,
        event_id=event_id,
        hlc=hlc,
    )
    assert recovered["delivered"] == 1
    message = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
    assert message["event_id"] == event_id
    assert message["cursor"] == {"hlc": hlc, "event_id": event_id}


def _seed_processing_changefeed_lease(*, event_id: str, lease_seconds: int) -> int:
    import boto3

    from hindsight.aws import aws_client_config

    now = int(time.time())
    lease_expires_at = now + lease_seconds
    table = boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(_required_env("HINDSIGHT_CHANGEFEED_IDEMPOTENCY_TABLE"))
    table.put_item(
        Item={
            "event_id": event_id,
            "state": "processing",
            "lease_owner": "hosted-acceptance-dead-owner",
            "lease_expires_at": lease_expires_at,
            "attempt_count": 1,
            "started_at": now,
            "updated_at": now,
            "expires_at": now + 24 * 60 * 60,
        }
    )
    return lease_expires_at


def _inject_changefeed_event(
    *,
    api_url: str,
    token: str,
    namespace: str,
    event_id: str | None = None,
    hlc: str | None = None,
    expected_accepted: int = 1,
) -> dict[str, object]:
    event_id = event_id or str(uuid4())
    hlc = hlc or f"{time.time_ns()}.0000000000"
    response = _post_json(
        f"{api_url}/internal/changefeed",
        token=token,
        payload=_changefeed_event_payload(namespace=namespace, event_id=event_id, hlc=hlc),
    )
    assert response["accepted"] == expected_accepted
    return {**response, "event_id": event_id, "hlc": hlc}


def _post_changefeed_event(
    *, api_url: str, token: str, namespace: str, event_id: str, hlc: str
) -> tuple[int, dict[str, object]]:
    body = json.dumps(
        _changefeed_event_payload(namespace=namespace, event_id=event_id, hlc=hlc)
    ).encode()
    req = request.Request(
        f"{api_url}/internal/changefeed",
        data=body,
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:  # noqa: S310 - fixed hosted URL
            return response.status, json.loads(response.read())
    except error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _changefeed_event_payload(*, namespace: str, event_id: str, hlc: str) -> dict[str, object]:
    return {
        "payload": [
            {
                "topic": "tenant_event_outbox",
                "updated": hlc,
                "after": {
                    "id": event_id,
                    "tenant_id": PUBLIC_DEMO_TENANT_ID,
                    "aggregate_type": "agent_runs",
                    "topics": [f"tenant:{PUBLIC_DEMO_TENANT_ID}:namespace:{namespace}"],
                    "payload": {"run_id": event_id, "status": "triaging"},
                },
            }
        ]
    }


def _post_json(
    url: str,
    *,
    token: str,
    payload: dict[str, object],
    extra_headers: dict[str, str] | None = None,
) -> dict[str, object]:
    body = json.dumps(payload).encode()
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            **(extra_headers or {}),
        },
    )
    try:
        with request.urlopen(req, timeout=30) as response:  # noqa: S310 - fixed hosted URL
            return json.loads(response.read())
    except error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise AssertionError(f"hosted API returned {exc.code}: {detail}") from exc


def _public_websocket_url(*, api_url: str, websocket_url: str) -> str:
    req = request.Request(
        f"{api_url.rstrip('/')}/v1/realtime/ticket",
        data=b"",
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as response:  # noqa: S310 - fixed hosted URL
            ticket = str(json.loads(response.read())["ticket"])
    except error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise AssertionError(f"hosted API returned {exc.code}: {detail}") from exc
    return f"{websocket_url}?{urlencode({'ticket': ticket})}"


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AssertionError(f"{name} is required for hosted acceptance")
    return value
