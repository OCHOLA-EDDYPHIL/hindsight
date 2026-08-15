"""Tests for the memory poisoning and rewind demo."""

import os
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
ROOT = Path(__file__).resolve().parents[1]


def test_demo_poison_precomputes_embedding_before_atomic_supersession(monkeypatch):
    from hindsight import demo_state

    events = []
    prepared_embedding = [0.0] * 1024
    seed_id = uuid4()
    belief_id = uuid4()
    seed = {
        "id": seed_id,
        "belief_id": belief_id,
        "writer": "demo.seed",
        "metadata": {
            "demo": "compromised-guidance-rewind",
            "role": "known-good",
        },
    }
    poisoned = {"id": uuid4(), "belief_id": belief_id, "previous_version_id": seed_id}

    provider = MagicMock()

    def embed_document(content):
        assert content == demo_state.COMPROMISED_GUIDANCE_CONTENT
        events.append("embed")
        return prepared_embedding

    provider.embed_document.side_effect = embed_document
    transaction = MagicMock()
    transaction.__enter__.side_effect = lambda: events.append("transaction.begin")
    transaction.__exit__.side_effect = lambda exc_type, *_args: events.append(
        "transaction.rollback" if exc_type else "transaction.commit"
    )
    connection = MagicMock()
    connection.transaction.return_value = transaction

    def execute(query, params):
        assert "close_active_demo_seed_for_supersession" in query
        assert params == (seed_id, "test:demo")
        events.append("close")
        result = MagicMock()
        result.fetchone.return_value = (seed_id,)
        return result

    connection.execute.side_effect = execute
    store = MagicMock()
    store.list_current_semantic.return_value = [seed]

    def write_semantic(**kwargs):
        assert kwargs["precomputed_embedding"] is prepared_embedding
        assert kwargs["belief_id"] == str(belief_id)
        assert kwargs["previous_version_id"] == str(seed_id)
        events.append("write")
        return poisoned

    store.write_semantic.side_effect = write_semantic

    def fake_connect(*_args, **_kwargs):
        events.append("connect")
        return nullcontext(connection)

    monkeypatch.setattr(demo_state, "connect", fake_connect)
    monkeypatch.setattr(demo_state, "MemoryStore", MagicMock(return_value=store))

    result = demo_state.poison_demo_memory(
        namespace="test:demo",
        db_url="postgresql://test",
        embedding_provider=provider,
    )

    assert result is poisoned
    assert events == [
        "embed",
        "connect",
        "transaction.begin",
        "close",
        "write",
        "transaction.commit",
    ]


def test_demo_supersession_boundary_is_tenant_scoped_and_does_not_grant_table_update():
    sql = (ROOT / "migrations/0032_demo_supersession_boundary.sql").read_text()
    roles = (ROOT / "infra/db/roles.sql").read_text()
    agent_update_grant = roles.split("GRANT UPDATE ON TABLE", 1)[1].split(
        "TO hindsight_agent_writer;", 1
    )[0]

    assert "SECURITY DEFINER" in sql
    assert "public.current_hindsight_tenant_id()" in sql
    assert "session.demo_kind = 'compromised_guidance_rewind'" in sql
    assert "session.status = 'active'" in sql
    assert "writer = 'demo.seed'" in sql
    assert "metadata->>'role' = 'known-good'" in sql
    assert "REVOKE ALL ON FUNCTION" in sql
    assert "GRANT EXECUTE ON FUNCTION" in sql
    assert "semantic_memories" not in agent_update_grant


@requires_db
def test_restricted_api_role_can_only_use_the_demo_supersession_boundary(monkeypatch):
    from hindsight import demo_state
    from hindsight.db import connect, database_url
    from tests.fakes import DeterministicEmbeddingProvider

    provider = DeterministicEmbeddingProvider()
    namespace = demo_state.reset_poison_rewind_state(
        namespace=f"restricted-demo:{uuid4()}",
        db_url=database_url(),
    )
    seed = demo_state.seed_good_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )

    with connect(database_url()) as conn:
        conn.execute((ROOT / "infra/db/roles.sql").read_text())
        conn.commit()
        conn.execute("SET ROLE hindsight_agent_writer")
        conn.commit()
        monkeypatch.setattr(
            demo_state,
            "connect",
            lambda *_args, **_kwargs: nullcontext(conn),
        )

        poisoned = demo_state.poison_demo_memory(
            namespace=namespace,
            db_url=database_url(),
            embedding_provider=provider,
        )
        conn.commit()

        assert poisoned["belief_id"] == seed["belief_id"]
        assert poisoned["previous_version_id"] == seed["id"]
        with pytest.raises(psycopg.errors.RaiseException):
            conn.execute(
                "SELECT close_active_demo_seed_for_supersession(%s, %s)",
                (poisoned["id"], namespace),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "UPDATE semantic_memories SET trust_status = trust_status WHERE id = %s",
                (poisoned["id"],),
            )
        conn.rollback()


@requires_db
def test_browser_demo_reset_isolates_sessions_and_incidents():
    from hindsight.db import database_url
    from hindsight.demo_state import (
        ensure_poison_rewind_incident,
        record_poison_rewind_anchor,
        reset_poison_rewind_state,
    )
    from hindsight.db import connect

    first_fixture = uuid4()
    second_fixture = uuid4()
    first_incident = ensure_poison_rewind_incident(
        fixture_id=first_fixture,
        db_url=database_url(),
    )
    second_incident = ensure_poison_rewind_incident(
        fixture_id=second_fixture,
        db_url=database_url(),
    )
    first = reset_poison_rewind_state(
        namespace=f"browser-reset:{uuid4()}",
        session_id=first_fixture,
        incident_id=first_fixture,
        db_url=database_url(),
    )
    rewind_anchor = record_poison_rewind_anchor(
        namespace=first,
        db_url=database_url(),
    )
    second = reset_poison_rewind_state(
        namespace=f"browser-reset:{uuid4()}",
        session_id=second_fixture,
        incident_id=second_fixture,
        db_url=database_url(),
    )
    replacement = reset_poison_rewind_state(
        namespace=first,
        db_url=database_url(),
    )
    with connect(database_url()) as conn:
        statuses = dict(
            conn.execute(
                "SELECT namespace, status FROM demo_sessions WHERE namespace IN (%s, %s, %s)",
                (first, second, replacement),
            ).fetchall()
        )
        replay_identity = conn.execute(
            """
                SELECT id, incident_tenant_id, incident_id, rewind_anchor
                FROM demo_sessions
                WHERE namespace = %s
            """,
            (first,),
        ).fetchone()
        session_tenant_id = replay_identity[1]
        conn.execute("DELETE FROM incidents WHERE id = %s", (second_fixture,))
        detached_identity = conn.execute(
            """
                SELECT tenant_id, incident_tenant_id, incident_id, status
                FROM demo_sessions
                WHERE namespace = %s
            """,
            (second,),
        ).fetchone()
        conn.commit()

    assert statuses == {first: "archived", second: "active", replacement: "active"}
    assert replay_identity == (
        first_fixture,
        session_tenant_id,
        first_fixture,
        rewind_anchor,
    )
    assert detached_identity == (session_tenant_id, None, None, "active")
    assert first_incident["id"] != second_incident["id"]
    assert first_incident["slug"] != second_incident["slug"]


@requires_db
def test_browser_signature_boundary_preserves_seed_and_closes_later_memories(monkeypatch):
    from hindsight import demo_state
    from hindsight.db import connect
    from hindsight.db import database_url
    from hindsight.demo_state import (
        poison_demo_memory,
        record_poison_rewind_anchor,
        reset_poison_rewind_state,
        seed_good_demo_memory,
        signature_replay_context,
    )
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.operations import enqueue_operation, execute_operation, preview_rewind

    provider = DeterministicEmbeddingProvider()
    namespace = reset_poison_rewind_state(
        namespace=f"browser-signature:{uuid4()}",
        db_url=database_url(),
    )
    seed = seed_good_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )
    rewind_anchor = record_poison_rewind_anchor(namespace=namespace, db_url=database_url())
    poison = poison_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )
    assert poison["metadata"]["scenario_role"] == "compromised_guidance"
    assert poison["metadata"]["risk_class"] == "stale_operational_guidance"
    assert "role" not in poison["metadata"]
    assert poison["belief_id"] == seed["belief_id"]
    assert poison["previous_version_id"] == seed["id"]
    assert poison["version_number"] == seed["version_number"] + 1
    assert poison["transition_kind"] == "supersession"
    with MemoryStore(
        url=database_url(),
        embedding_provider=provider,
    ) as store:
        rejected_reflection = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="Rejected certificate-rotation recommendation retained for audit.",
            provenance=Provenance(
                writer="agent.reflect",
                source_ref=f"browser-signature:{uuid4()}",
                justification="Retain the rejected recommendation in governed history",
            ),
        )

    preview = preview_rewind(
        namespace=namespace,
        target_timestamp=rewind_anchor,
        actor="test.operator",
        reason="Close memories written after the known-good boundary",
        db_url=database_url(),
    )
    operation, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=str(preview["fingerprint"]),
        idempotency_key=f"browser-signature:{uuid4()}",
        db_url=database_url(),
    )
    completed = execute_operation(
        operation_id=str(operation["id"]),
        embedding_provider=provider,
        worker_id="test.browser-signature",
        db_url=database_url(),
    )
    with connect(database_url()) as restricted_conn:
        restricted_conn.execute((ROOT / "infra/db/roles.sql").read_text())
        restricted_conn.commit()
        restricted_conn.execute("SET ROLE hindsight_memory_worker")
        restricted_conn.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            restricted_conn.execute(
                "UPDATE demo_sessions SET status = status WHERE namespace = %s",
                (namespace,),
            )
        restricted_conn.rollback()
        with monkeypatch.context() as replay_patch:
            replay_patch.setattr(
                demo_state,
                "connect",
                lambda *_args, **_kwargs: nullcontext(restricted_conn),
            )
            replay_context = signature_replay_context(
                namespace=namespace,
                db_url=database_url(),
            )
        restricted_conn.execute("RESET ROLE")
        restricted_conn.commit()

    invalidated_ids = {str(value) for value in completed["invalidated_memory_ids"]}
    assert invalidated_ids == {
        str(poison["id"]),
        str(rejected_reflection["id"]),
    }
    assert replay_context is not None
    assert replay_context["correction_operation"]["id"] == str(completed["id"])
    assert replay_context["correction_operation"]["invalidated_memory_ids"] == [
        str(value) for value in completed["invalidated_memory_ids"]
    ]
    assert replay_context["correction_operation"]["restored_memory_ids"] == [
        str(value) for value in completed["restored_memory_ids"]
    ]
    assert [
        effect["sequence"] for effect in replay_context["correction_operation"]["effects"]
    ] == list(range(1, len(replay_context["correction_operation"]["effects"]) + 1))
    with MemoryStore(url=database_url()) as store:
        current = store.list_current_semantic(namespace=namespace, limit=100)
        poison_audit = store.audit_memory(
            memory_kind="semantic",
            memory_id=str(poison["id"]),
        )
        seed_audit = store.audit_memory(
            memory_kind="semantic",
            memory_id=str(seed["id"]),
        )
        reflection_audit = store.audit_memory(
            memory_kind="semantic",
            memory_id=str(rejected_reflection["id"]),
        )

    assert len(current) == 1
    reasserted = current[0]
    assert reasserted["belief_id"] == seed["belief_id"]
    assert reasserted["version_number"] == poison["version_number"] + 1
    assert reasserted["previous_version_id"] == poison["id"]
    assert reasserted["transition_kind"] == "rewind_reassertion"
    assert str(reasserted["id"]) in {str(value) for value in completed["restored_memory_ids"]}
    assert seed_audit is not None and seed_audit["t_invalid"] is not None
    assert poison_audit is not None and poison_audit["t_invalid"] is not None
    assert reflection_audit is not None and reflection_audit["t_invalid"] is not None
