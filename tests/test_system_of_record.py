"""Database-backed tests for transactional incident data."""

import os
import pathlib

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEMO_FIXTURE = ROOT / "fixtures" / "demo_incidents.sql"
SHOWCASE_QUERY = ROOT / "queries" / "showcase_similar_incidents.sql"


@pytest.fixture
def seeded_conn():
    from hindsight.db import connect

    conn = connect()
    try:
        with conn.transaction():
            conn.execute(DEMO_FIXTURE.read_text())
            yield conn
            raise RuntimeError("rollback fixture data")
    except RuntimeError as exc:
        if str(exc) != "rollback fixture data":
            raise
    finally:
        conn.close()


@requires_db
def test_system_of_record_schema_objects_exist():
    from hindsight.db import connect

    expected = {
        "services",
        "incidents",
        "incident_services",
        "incident_events",
        "runbooks",
        "incident_runbooks",
        "incident_semantic_memories",
    }
    with connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                        AND table_type = 'BASE TABLE'
                """
            )
        }

    assert expected <= tables


@requires_db
def test_demo_fixture_loads_believable_incident_data(seeded_conn):
    assert seeded_conn.execute("SELECT count(*) FROM services").fetchone() == (3,)
    assert seeded_conn.execute("SELECT count(*) FROM incidents").fetchone() == (3,)
    assert seeded_conn.execute("SELECT count(*) FROM runbooks").fetchone() == (3,)
    assert seeded_conn.execute("SELECT count(*) FROM incident_events").fetchone() == (6,)

    row = seeded_conn.execute(
        """
            SELECT i.title, s.slug, r.slug
            FROM incidents AS i
            JOIN incident_services AS isvc ON isvc.incident_id = i.id
            JOIN services AS s ON s.id = isvc.service_id
            JOIN incident_runbooks AS ir ON ir.incident_id = i.id
            JOIN runbooks AS r ON r.id = ir.runbook_id
            WHERE i.slug = 'inc-payment-latency-2026-06-14'
        """
    ).fetchone()

    assert row == (
        "Payment authorization latency above checkout SLO",
        "payments-api",
        "payments-latency-triage",
    )


@requires_db
def test_showcase_query_joins_vectors_validity_and_transactional_filters(seeded_conn):
    from hindsight.embeddings import DeterministicEmbeddingProvider, vector_literal
    from hindsight.memory import MemoryStore, Provenance

    provider = DeterministicEmbeddingProvider()
    store = MemoryStore(seeded_conn, embedding_provider=provider)
    namespace = "demo-payment-incident"
    payment_memory = store.write_semantic(
        namespace=namespace,
        content="Payment latency improved after throttling retry fanout to the processor.",
        provenance=Provenance(
            writer="agent.reflect",
            source_ref="inc-payment-latency-2026-06-14",
            justification="Demo memory for similar incident retrieval",
        ),
    )
    stale_memory = store.write_semantic(
        namespace=namespace,
        content="Certificate rotation caused the payment latency.",
        provenance=Provenance(
            writer="agent.reflect",
            source_ref="inc-payment-latency-2026-06-14:stale",
            justification="Invalidated demo memory",
        ),
    )
    gateway_memory = store.write_semantic(
        namespace=namespace,
        content="Edge gateway served an expired certificate bundle.",
        provenance=Provenance(
            writer="agent.reflect",
            source_ref="inc-edge-cert-expiry-2026-06-21",
            justification="Different service memory",
        ),
    )

    seeded_conn.execute(
        """
            INSERT INTO incident_semantic_memories (incident_id, memory_id, relationship)
            VALUES
                ('30000000-0000-0000-0000-000000000001', %s, 'resolution'),
                ('30000000-0000-0000-0000-000000000001', %s, 'lesson'),
                ('30000000-0000-0000-0000-000000000002', %s, 'root_cause')
        """,
        (payment_memory["id"], stale_memory["id"], gateway_memory["id"]),
    )
    store.invalidate(
        memory_kind="semantic",
        memory_id=str(stale_memory["id"]),
        invalidated_by="agent.rewind",
        reason="Showcase query must exclude invalidated memories",
    )

    query_vector = vector_literal(provider.embed("payment retry fanout latency"))
    rows = seeded_conn.execute(
        SHOWCASE_QUERY.read_text(),
        (query_vector, namespace, "payments-api", query_vector, 5),
    ).fetchall()

    assert len(rows) == 1
    assert rows[0][0] == payment_memory["id"]
    assert rows[0][3] == "inc-payment-latency-2026-06-14"
    assert rows[0][6] == "payments-api"
    assert rows[0][8] == "payments-latency-triage"
