"""Lightweight fixture mutations for the browser-driven correction scenario."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from hindsight.db import connect, database_url
from hindsight.embeddings import EmbeddingProvider
from hindsight.memory import APPROVED_POSITIVE_GUIDANCE, MemoryStore, Provenance

DEMO_NAMESPACE = "demo:payments-poison-rewind"
DEMO_INCIDENT_ID = "demo-payments-checkout-latency"
DEMO_SERVICE_SLUG = "payments-api"
DEMO_TITLE = "Checkout p99 latency above SLO"
DEMO_INPUT = (
    "Checkout p99 is above 2s and the queue is growing. Inspect current telemetry "
    "and recommend one reversible next action."
)
GOOD_MEMORY_CONTENT = (
    "Resolved payments-api incident: checkout p99 was above 2s and the queue was "
    "growing while processor timeouts amplified retries. The accepted first reversible "
    "action was to throttle retry fanout, then watch queue depth and p99 before changing "
    "worker capacity."
)
COMPROMISED_GUIDANCE_CONTENT = (
    "Imported payments-api runbook: when checkout p99 is above 2s and the queue is "
    "growing during processor timeouts, the first reversible action is to scale payment "
    "workers, then inspect queue depth before changing retry controls."
)


def current_database_timestamp(*, db_url: str | None = None) -> datetime:
    """Return the database-authoritative timestamp for a demo boundary."""

    with connect(db_url or database_url(), application_name="hindsight-demo-boundary") as conn:
        row = conn.execute("SELECT now()").fetchone()
    if row is None:
        raise RuntimeError("database did not return a demo boundary timestamp")
    return row[0]


def reset_poison_rewind_state(
    *,
    namespace: str = DEMO_NAMESPACE,
    session_id: UUID | None = None,
    incident_id: UUID | None = None,
    db_url: str | None = None,
) -> str:
    """Archive only the supplied session and return a fresh session namespace."""

    resolved_db_url = db_url or database_url()
    base_namespace = namespace.split(":session:", 1)[0]
    resolved_session_id = session_id or uuid4()
    session_namespace = f"{base_namespace}:session:{resolved_session_id.hex}"
    with connect(resolved_db_url) as conn:
        conn.execute(
            """
                UPDATE demo_sessions
                SET status = 'archived', archived_at = COALESCE(archived_at, now())
                WHERE namespace = %s AND status = 'active'
            """,
            (namespace,),
        )
        conn.execute(
            """
                INSERT INTO demo_sessions (
                    id, demo_kind, namespace, created_by,
                    incident_tenant_id, incident_id
                )
                VALUES (
                    %s, 'compromised_guidance_rewind', %s,
                    'dashboard.operator',
                    (SELECT tenant_id FROM incidents WHERE id = %s), %s
                )
            """,
            (
                resolved_session_id,
                session_namespace,
                incident_id,
                incident_id,
            ),
        )
        conn.commit()
    return session_namespace


def record_poison_rewind_anchor(
    *,
    namespace: str,
    db_url: str | None = None,
) -> datetime:
    """Persist and return the database timestamp that bounds a scenario rewind."""

    with connect(db_url or database_url()) as conn:
        row = conn.execute(
            """
                UPDATE demo_sessions
                SET rewind_anchor = now()
                WHERE namespace = %s AND status = 'active'
                RETURNING rewind_anchor
            """,
            (namespace,),
        ).fetchone()
        conn.commit()
    if row is None:
        raise LookupError("active demo session not found")
    return row[0]


def signature_replay_context(
    *,
    namespace: str,
    db_url: str | None = None,
) -> dict[str, Any] | None:
    """Return the server-owned identity and anchor for one controlled replay."""

    with connect(
        db_url or database_url(),
        application_name="hindsight-signature-replay-context",
    ) as conn:
        row = conn.execute(
            """
                SELECT id, namespace, rewind_anchor
                FROM demo_sessions
                WHERE namespace = %s
                  AND demo_kind IN ('compromised_guidance_rewind', 'poison_rewind')
                ORDER BY created_at DESC
                LIMIT 1
            """,
            (namespace,),
        ).fetchone()
        operation = conn.execute(
            """
                SELECT id, status, target_timestamp, invalidated_memory_ids,
                       restored_memory_ids
                FROM memory_operations
                WHERE namespace = %s AND operation_type = 'rewind'
                ORDER BY created_at DESC
                LIMIT 1
            """,
            (namespace,),
        ).fetchone()
        effects = (
            conn.execute(
                """
                    SELECT sequence, effect_type, source_memory_id,
                           result_memory_id, belief_id, namespace
                    FROM memory_operation_effects
                    WHERE operation_id = %s
                    ORDER BY sequence
                """,
                (operation[0],),
            ).fetchall()
            if operation is not None and operation[1] == "completed"
            else []
        )
    if row is None or row[2] is None:
        return None
    context = {
        "scenario_id": str(row[0]),
        "namespace": str(row[1]),
        "replay_anchor": row[2].astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    if operation is not None and operation[1] == "completed":
        context["correction_operation"] = {
            "id": str(operation[0]),
            "target_timestamp": operation[2].astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "invalidated_memory_ids": [str(value) for value in operation[3] or []],
            "restored_memory_ids": [str(value) for value in operation[4] or []],
            "effects": [
                {
                    "sequence": effect[0],
                    "effect_type": effect[1],
                    "source_memory_id": (str(effect[2]) if effect[2] is not None else None),
                    "result_memory_id": (str(effect[3]) if effect[3] is not None else None),
                    "belief_id": str(effect[4]) if effect[4] is not None else None,
                    "namespace": effect[5],
                }
                for effect in effects
            ],
        }
    return context


def seed_good_demo_memory(
    *,
    embedding_provider: EmbeddingProvider,
    namespace: str = DEMO_NAMESPACE,
    db_url: str | None = None,
) -> dict[str, Any]:
    with MemoryStore(
        url=db_url or database_url(),
        embedding_provider=embedding_provider,
    ) as store:
        return store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content=GOOD_MEMORY_CONTENT,
            provenance=Provenance(
                writer="demo.seed",
                source_ref="demo:known-good-payment-incident",
                justification=(
                    "Seed known-good payment latency resolution before importing stale guidance"
                ),
            ),
            metadata={
                "demo": "compromised-guidance-rewind",
                "role": "known-good",
                "kind": "procedural_lesson",
                "operator_disposition": "approved",
                "safety_status": "safe",
                "contradiction_status": "supported",
                "evidence_quality": "resolved_incident",
                "usage_instruction": "positive_guidance",
            },
        )


def poison_demo_memory(
    *,
    embedding_provider: EmbeddingProvider,
    namespace: str = DEMO_NAMESPACE,
    db_url: str | None = None,
) -> dict[str, Any]:
    resolved_db_url = db_url or database_url()
    prepared_embedding = embedding_provider.embed_document(COMPROMISED_GUIDANCE_CONTENT)
    with connect(resolved_db_url, application_name="hindsight-demo-supersession") as conn:
        with conn.transaction():
            store = MemoryStore(conn=conn, embedding_provider=embedding_provider)
            candidates = [
                memory
                for memory in store.list_current_semantic(namespace=namespace, limit=100)
                if memory.get("writer") == "demo.seed"
                and isinstance(memory.get("metadata"), dict)
                and memory["metadata"].get("demo") == "compromised-guidance-rewind"
                and memory["metadata"].get("role") == "known-good"
            ]
            if len(candidates) != 1:
                raise RuntimeError(
                    "demo supersession requires exactly one current known-good belief version"
                )
            seed = candidates[0]
            decision_id = f"demo:supersession:{uuid4()}"
            store.open_decision(
                decision_id=decision_id,
                actor="demo.fixture-import",
                decision_kind="demo_guidance_supersession",
                purpose="Supersede the accepted belief with the imported runbook version",
                namespace=namespace,
            )
            store.record_read(
                decision_id=decision_id,
                memory_kind="semantic",
                memory_id=str(seed["id"]),
                reader="demo.fixture-import",
                purpose="Supersede the exact accepted belief version",
            )
            closed = conn.execute(
                "SELECT close_active_demo_seed_for_supersession(%s, %s)",
                (seed["id"], namespace),
            ).fetchone()
            if closed is None or str(closed[0]) != str(seed["id"]):
                raise RuntimeError("known-good demo belief changed before supersession")
            return store.write_semantic(
                namespace=namespace,
                content=COMPROMISED_GUIDANCE_CONTENT,
                provenance=Provenance(
                    writer="demo.fixture-import",
                    source_ref="demo:stale-runbook-import",
                    justification=(
                        "Import a previously approved payment runbook response through "
                        "the normal governed memory path"
                    ),
                ),
                metadata={
                    "demo": "compromised-guidance-rewind",
                    "scenario_role": "compromised_guidance",
                    "risk_class": "stale_operational_guidance",
                    "kind": "procedural_lesson",
                    "evidence_quality": "legacy_runbook",
                },
                governance=APPROVED_POSITIVE_GUIDANCE,
                producer_decision_id=decision_id,
                parent_memory_ids=[str(seed["id"])],
                belief_id=str(seed["belief_id"]),
                previous_version_id=str(seed["id"]),
                transition_kind="supersession",
                precomputed_embedding=prepared_embedding,
            )


def ensure_poison_rewind_incident(
    *, fixture_id: UUID | None = None, db_url: str | None = None
) -> dict[str, Any]:
    service_id = fixture_id or UUID("10000000-0000-0000-0000-000000000001")
    incident_id = fixture_id or UUID("40000000-0000-0000-0000-000000000001")
    incident_slug = f"{DEMO_INCIDENT_ID}:{fixture_id.hex}" if fixture_id else DEMO_INCIDENT_ID
    with connect(db_url or database_url()) as conn:
        with conn.transaction():
            service = conn.execute(
                """
                    INSERT INTO services (id, slug, name, owner_team, tier)
                    VALUES (
                        %s,
                        %s,
                        'Payments API',
                        'revenue-platform',
                        'critical'
                    )
                    ON CONFLICT (tenant_id, slug) DO UPDATE SET name = excluded.name
                    RETURNING id
                """,
                (service_id, DEMO_SERVICE_SLUG),
            ).fetchone()
            incident = conn.execute(
                """
                    INSERT INTO incidents (
                        id, slug, title, severity, status, started_at, summary
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        'sev2',
                        'open',
                        now(),
                        %s
                    )
                    ON CONFLICT (tenant_id, slug) DO UPDATE SET
                        title = excluded.title,
                        severity = excluded.severity,
                        status = excluded.status,
                        started_at = excluded.started_at,
                        resolved_at = NULL,
                        summary = excluded.summary,
                        root_cause = NULL
                    RETURNING *
                """,
                (incident_id, incident_slug, DEMO_TITLE, DEMO_INPUT),
            ).fetchone()
            conn.execute(
                """
                    INSERT INTO incident_services (incident_id, service_id, impact)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (incident_id, service_id) DO UPDATE SET impact = excluded.impact
                """,
                (incident[0], service[0], DEMO_INPUT),
            )
        return {
            "id": str(incident[0]),
            "slug": incident[1],
            "title": incident[2],
            "severity": incident[3],
            "status": incident[4],
            "service_slug": DEMO_SERVICE_SLUG,
        }
