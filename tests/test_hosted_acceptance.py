"""Opt-in end-to-end acceptance against the deployed demo control plane."""

from __future__ import annotations

import asyncio
import json
import os
import time
from urllib import error, request
from uuid import uuid4

import pytest


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
def test_websocket_requires_resubscribe_after_reconnect_and_honors_unsubscribe():
    from websockets.asyncio.client import connect

    api_url = _required_env("HOSTED_API_URL").rstrip("/")
    websocket_url = _required_env("HINDSIGHT_WEBSOCKET_URL")
    changefeed_token = _required_env("HINDSIGHT_CHANGEFEED_AUTH_TOKEN")
    namespace = f"live-websocket-lifecycle:{uuid4().hex}"

    async def scenario() -> None:
        first = await connect(websocket_url, open_timeout=15, close_timeout=15)
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

        async with connect(websocket_url, open_timeout=15, close_timeout=15) as second:
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
            assert message["namespace"] == namespace
            assert message["data"]["run"]["id"] == result["event_id"]
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
        assert message["data"]["run"]["id"] == result["event_id"]
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
                    "topic": "agent_runs",
                    "after": {
                        "id": event_id,
                        "namespace": namespace,
                        "status": "triaging",
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


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AssertionError(f"{name} is required for hosted acceptance")
    return value
