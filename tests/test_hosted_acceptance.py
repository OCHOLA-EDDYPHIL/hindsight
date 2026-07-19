"""Opt-in end-to-end acceptance against the deployed demo control plane."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from urllib import error, request
from urllib.parse import urlencode
from uuid import uuid4

import pytest

from hindsight.server_tenants import PUBLIC_DEMO_TENANT_ID


requires_hosted_acceptance = pytest.mark.skipif(
    os.environ.get("RUN_HOSTED_ACCEPTANCE") != "1",
    reason="hosted acceptance is opt-in",
)


@requires_hosted_acceptance
def test_resolved_transition_reaches_managed_changefeed_worker_and_cited_lesson():
    from hindsight.db import connect
    from hindsight.embeddings import embedding_provider_from_env
    from hindsight.gemini import gemini_pool_from_env
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.runs import create_incident
    from hindsight.runtime import runtime_settings

    api_url = _required_env("HOSTED_API_URL").rstrip("/")
    operator_token = _required_env("HINDSIGHT_BROWSER_OPERATOR_TOKEN")
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
        f"{api_url}/v1/incidents/{slug}/resolution",
        token=operator_token,
        payload={
            "root_cause": "Retry amplification overloaded an unhealthy payment processor.",
            "action": "Inspect processor health and throttle retry fanout.",
            "observation": "Timeout rate and queue depth recovered after fanout was throttled.",
            "recovered": True,
        },
    )
    event_id = str(resolution["event"]["id"])

    deadline = time.monotonic() + 600
    job = None
    while time.monotonic() < deadline:
        with connect(
            settings.database_url,
            application_name="hindsight-hosted-acceptance",
        ) as conn:
            row = conn.execute(
                """
                    SELECT id, source_event_id, decision_id, status, lesson_memory_id,
                           error_code, error_detail
                    FROM consolidation_jobs
                    WHERE incident_id = %s AND source_event_id = %s
                """,
                (incident["id"], event_id),
            ).fetchone()
        if row is not None:
            job = row
            if row[3] == "completed":
                break
            if row[3] in {"failed", "not_eligible"}:
                pytest.fail(
                    "managed consolidation terminated without a lesson: "
                    f"status={row[3]} code={row[5]} detail={row[6]}"
                )
        time.sleep(2)

    assert job is not None, "managed changefeed never created a consolidation job"
    assert job[3] == "completed", "managed consolidation did not complete before timeout"
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
    assert lesson[5] == "active"
    assert lesson[6] is None
    assert source_read == 1
    assert lesson_link == ("lesson",)

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
            purpose="Verify the asynchronously published lesson is usable by semantic recall",
            policy="semantic_strict",
            limit=5,
        )
        store.seal_decision(decision_id=retrieval_decision)
    assert retrieval.selected_strategy == "semantic_vector"
    assert str(job[4]) in {str(hit["id"]) for hit in retrieval.hits}


@requires_hosted_acceptance
def test_scheduled_dispatch_reclaims_expired_attempt_and_finalizes_dlq():
    from hindsight.db import connect
    from hindsight.runs import (
        claim_run_attempt,
        create_incident,
        create_run,
        prepare_approval,
    )
    from hindsight.runtime import runtime_database_url

    database_url = runtime_database_url()
    token = _acceptance_token("worker")
    lease_seconds = _positive_int_env(
        "HINDSIGHT_ACCEPTANCE_RUN_ATTEMPT_LEASE_SECONDS"
    )
    visibility_seconds = _positive_int_env(
        "HINDSIGHT_ACCEPTANCE_QUEUE_VISIBILITY_SECONDS"
    )
    max_attempts = _positive_int_env("HINDSIGHT_ACCEPTANCE_RUN_MAX_ATTEMPTS")
    scheduler_seconds = _positive_int_env("HINDSIGHT_ACCEPTANCE_SCHEDULER_SECONDS")
    assert max_attempts == 3

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
    prepare_approval(run_id=pending["id"], approved=True, db_url=database_url)
    _wait_for_run_status(
        str(pending["id"]),
        expected={"completed"},
        database_url=database_url,
        timeout=scheduler_seconds * 2 + lease_seconds + 180,
    )

    reclaimed_slug = f"hosted-reclaim:{token}"
    create_incident(
        slug=reclaimed_slug,
        title="Expired attempt reclaim validation",
        severity="sev3",
        summary="Verify a naturally expired attempt is reclaimed.",
        db_url=database_url,
    )
    reclaimed, _ = create_run(
        incident_slug=reclaimed_slug,
        namespace=f"hosted-reclaim:{token}",
        user_input="Verify recovery after an expired worker attempt",
        dispatch_available_at=datetime.now(UTC) + timedelta(seconds=5),
        db_url=database_url,
    )
    first = claim_run_attempt(
        run_id=reclaimed["id"],
        command="start",
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

    exhausted_slug = f"hosted-dlq:{token}"
    create_incident(
        slug=exhausted_slug,
        title="Dead-letter finalization validation",
        severity="sev3",
        summary="Verify exhausted attempts finalize through the hosted DLQ.",
        db_url=database_url,
    )
    exhausted, _ = create_run(
        incident_slug=exhausted_slug,
        namespace=f"hosted-dlq:{token}",
        user_input="Verify exhausted source delivery is finalized from the DLQ",
        dispatch_available_at=(
            datetime.now(UTC) + timedelta(seconds=max_attempts * 2 + 5)
        ),
        db_url=database_url,
    )
    for expected_attempt in range(1, max_attempts + 1):
        claim = claim_run_attempt(
            run_id=exhausted["id"],
            command="start",
            lease_ttl=timedelta(seconds=1),
            max_attempts=max_attempts,
            db_url=database_url,
        )
        assert claim.outcome == "claimed"
        assert claim.run["worker_attempt_count"] == expected_attempt
        time.sleep(1.1)
    failed = _wait_for_run_status(
        str(exhausted["id"]),
        expected={"failed"},
        database_url=database_url,
        timeout=(
            scheduler_seconds * 2
            + visibility_seconds * (max_attempts + 1)
            + 120
        ),
    )
    assert failed["failure_code"] == "RunAttemptsExhausted"
    assert [event["status"] for event in failed["events"]].count("failed") == 1
    with connect(database_url, application_name="hindsight-hosted-acceptance") as conn:
        decision = conn.execute(
            "SELECT status, sealed_at IS NOT NULL FROM memory_decisions WHERE id = %s",
            (failed["decision_id"],),
        ).fetchone()
    assert decision == ("failed", True)


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
        await first.send(
            json.dumps({"type": "subscribe", "namespace": namespace, "run_id": None})
        )
        await _wait_for_websocket_delivery(
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


async def _wait_for_websocket_delivery(
    socket, *, api_url: str, token: str, namespace: str
) -> None:
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
            assert message["tenant_id"] == PUBLIC_DEMO_TENANT_ID
            assert message["topic_keys"] == [
                f"tenant:{PUBLIC_DEMO_TENANT_ID}:namespace:{namespace}"
            ]
            assert message["data"]["reference"]["run_id"] == result["event_id"]
            return
        assert result["delivered"] == 0
        await asyncio.sleep(0.5)
    pytest.fail("WebSocket subscription did not become active")


async def _wait_for_websocket_silence(
    socket, *, api_url: str, token: str, namespace: str
) -> None:
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


def _inject_changefeed_event(
    *, api_url: str, token: str, namespace: str
) -> dict[str, object]:
    event_id = str(uuid4())
    response = _post_json(
        f"{api_url}/internal/changefeed",
        token=token,
        payload={
            "payload": [
                {
                    "topic": "tenant_event_outbox",
                    "after": {
                        "id": event_id,
                        "tenant_id": PUBLIC_DEMO_TENANT_ID,
                        "aggregate_type": "agent_runs",
                        "topics": [
                            f"tenant:{PUBLIC_DEMO_TENANT_ID}:namespace:{namespace}"
                        ],
                        "payload": {"run_id": event_id, "status": "triaging"},
                    },
                }
            ]
        },
    )
    assert response["accepted"] == 1
    return {**response, "event_id": event_id}


def _post_json(url: str, *, token: str, payload: dict[str, object]) -> dict[str, object]:
    body = json.dumps(payload).encode()
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
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
