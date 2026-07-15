"""Tests for the CockroachDB-backed incident agent graph."""

import os
import pathlib
import asyncio
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKPOINT_QUERY = ROOT / "queries" / "agent_checkpoint_rows.sql"
CHAT_QUERY = ROOT / "queries" / "agent_chat_history.sql"


def test_async_sqlalchemy_url_uses_async_psycopg_driver():
    from hindsight.agent import _async_sqlalchemy_url

    assert _async_sqlalchemy_url("postgresql://root@localhost/db") == (
        "cockroachdb+psycopg://root@localhost/db"
    )
    assert _async_sqlalchemy_url("cockroachdb://root@localhost/db") == (
        "cockroachdb+psycopg://root@localhost/db"
    )


def test_recall_does_not_silently_fall_back_after_vector_error(monkeypatch):
    import hindsight.agent as agent
    from hindsight.embeddings import DeterministicEmbeddingProvider

    calls = []

    class FakeMemoryStore:
        def __init__(self, *, url, embedding_provider=None):
            self.embedding_provider = embedding_provider
            calls.append(embedding_provider is not None)

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def retrieve_semantic(self, **kwargs):
            raise RuntimeError("semantic_memory_embeddings is missing")

    monkeypatch.setattr(agent, "MemoryStore", FakeMemoryStore)

    with pytest.raises(RuntimeError, match="governed retrieval failed"):
        agent._recall_for_state(
            {
                "namespace": "incident-test",
                "service_slug": "payments-api",
                "user_input": "checkout latency",
                "decision_id": "agent:test:plan",
            },
            db_url="postgresql://db",
            embedding_provider=DeterministicEmbeddingProvider(),
        )

    assert calls == [True]


def test_agent_storage_setup_is_cached(monkeypatch):
    import hindsight.agent as agent

    calls = {"checkpoint": 0, "chat": 0}

    class FakeSaver:
        @classmethod
        def from_conn_string(cls, db_url):
            assert db_url == "postgresql://db"
            return cls()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def setup(self):
            calls["checkpoint"] += 1

    class FakeHistory:
        def create_table_if_not_exists(self):
            calls["chat"] += 1

        def close(self):
            pass

    monkeypatch.setattr(agent, "CockroachDBSaver", FakeSaver)
    monkeypatch.setattr(agent, "_chat_history", lambda **kwargs: FakeHistory())
    agent._SETUP_DB_URLS.clear()

    agent._setup_agent_storage_once("postgresql://db")
    agent._setup_agent_storage_once("postgresql://db")

    assert calls == {"checkpoint": 1, "chat": 1}


def test_sync_agent_entrypoint_rejects_running_event_loop():
    from hindsight.agent import IncidentInput, run_incident_agent

    async def call_sync_helper():
        with pytest.raises(RuntimeError, match="synchronous helpers"):
            run_incident_agent(
                IncidentInput(user_input="latency", incident_id="incident-1"),
                db_url="postgresql://db",
            )

    asyncio.run(call_sync_helper())


def test_async_run_incident_agent_wraps_sync_graph(monkeypatch):
    import hindsight.agent as agent
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import DeterministicReasoningProvider

    calls = []
    reasoning_provider = DeterministicReasoningProvider(response_text="plan")
    embedding_provider = DeterministicEmbeddingProvider()

    def fake_invoke_graph(input_or_command, **kwargs):
        calls.append((input_or_command, kwargs))
        return {
            "plan": "check dependencies",
            "proposed_action": "review rollback",
            "reflected_memory": {"id": "memory-1"},
        }

    monkeypatch.setattr(agent, "_invoke_graph", fake_invoke_graph)

    async def call_async_helper():
        return await agent.run_incident_agent_async(
            agent.IncidentInput(
                user_input="latency",
                incident_id="incident-1",
                namespace="namespace-1",
            ),
            thread_id="thread-1",
            pause_before_act=True,
            db_url="postgresql://db",
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
        )

    result = asyncio.run(call_async_helper())

    assert result.thread_id == "thread-1"
    assert result.plan == "check dependencies"
    assert result.reflected_memory_id == "memory-1"
    state, kwargs = calls[0]
    assert state["thread_id"] == "thread-1"
    assert state["incident_id"] == "incident-1"
    assert state["namespace"] == "namespace-1"
    assert state["pause_before_act"] is True
    assert state["run_id"]
    assert state["decision_id"] == f"agent:{state['run_id']}:plan"
    assert kwargs == {
        "thread_id": "thread-1",
        "db_url": "postgresql://db",
        "reasoning_provider": reasoning_provider,
        "embedding_provider": embedding_provider,
        "progress_callback": None,
    }


def test_async_resume_incident_agent_wraps_sync_graph(monkeypatch):
    import hindsight.agent as agent
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import DeterministicReasoningProvider

    calls = []
    reasoning_provider = DeterministicReasoningProvider(response_text="plan")
    embedding_provider = DeterministicEmbeddingProvider()

    def fake_invoke_graph(input_or_command, **kwargs):
        calls.append((input_or_command, kwargs))
        return {
            "action_approved": False,
            "proposed_action": "hold change",
            "reflected_memory": {"id": "memory-2"},
        }

    monkeypatch.setattr(agent, "_invoke_graph", fake_invoke_graph)

    async def call_async_helper():
        return await agent.resume_incident_agent_async(
            thread_id="thread-2",
            approved=False,
            db_url="postgresql://db",
            reasoning_provider=reasoning_provider,
            embedding_provider=embedding_provider,
        )

    result = asyncio.run(call_async_helper())

    assert result.thread_id == "thread-2"
    assert result.proposed_action == "hold change"
    assert result.reflected_memory_id == "memory-2"
    command, kwargs = calls[0]
    assert command.resume is False
    assert kwargs == {
        "thread_id": "thread-2",
        "db_url": "postgresql://db",
        "reasoning_provider": reasoning_provider,
        "embedding_provider": embedding_provider,
        "progress_callback": None,
    }


@requires_db
def test_incident_graph_checkpoints_and_reflects_to_memory():
    from hindsight.agent import IncidentInput, run_incident_agent
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import DeterministicReasoningProvider

    thread_id = f"agent-{uuid4()}"
    result = run_incident_agent(
        IncidentInput(
            user_input="payments-api checkout latency is above SLO after retry fanout",
            incident_id=f"incident-{uuid4()}",
            namespace=f"agent-test-{uuid4()}",
            service_slug="payments-api",
            severity="sev2",
            title="Checkout latency",
        ),
        thread_id=thread_id,
        reasoning_provider=DeterministicReasoningProvider(
            response_text="throttle retry fanout, check processor latency, watch checkout SLO"
        ),
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert not result.interrupted
    assert result.plan == "throttle retry fanout, check processor latency, watch checkout SLO"
    assert result.proposed_action is not None
    assert result.reflected_memory_id is not None

    with connect(database_url()) as conn:
        checkpoint_rows = conn.execute(CHECKPOINT_QUERY.read_text(), (thread_id,)).fetchall()
        chat_rows = conn.execute(CHAT_QUERY.read_text(), (thread_id,)).fetchall()
        reflection = conn.execute(
            """
                SELECT plan, proposed_action, semantic_memory_id, belief_id
                FROM agent_reflections WHERE decision_id = %s
            """,
            (result.state["decision_id"],),
        ).fetchone()

    assert checkpoint_rows
    assert {row[1] for row in chat_rows} == {"human", "ai"}
    assert "checkout latency" in chat_rows[0][2]
    assert reflection is not None
    assert reflection[0] == result.plan
    assert reflection[1] == result.proposed_action
    assert str(reflection[2]) == result.reflected_memory_id
    assert reflection[3] is not None


@requires_db
def test_reflection_projection_failure_rolls_back_semantic_output(monkeypatch):
    from hindsight.db import connect, database_url
    from hindsight.memory import MemoryStore, Provenance

    decision_id = f"reflection-rollback:{uuid4()}"
    namespace = f"reflection-rollback-{uuid4()}"
    conn = connect()
    try:
        with conn.transaction():
            store = MemoryStore(conn=conn)
            store.open_decision(
                decision_id=decision_id,
                actor="agent.reflect",
                decision_kind="agent_plan",
                purpose="verify atomic reflection persistence",
                namespace=namespace,
            )
        store = MemoryStore(conn=conn)

        def fail_projection(**kwargs):
            raise RuntimeError("projection failed")

        monkeypatch.setattr(store, "record_agent_reflection", fail_projection)
        with pytest.raises(RuntimeError, match="projection failed"):
            store.remember_agent_reflection(
                decision_id=decision_id,
                run_id=str(uuid4()),
                thread_id=f"thread-{uuid4()}",
                incident_id=f"incident-{uuid4()}",
                namespace=namespace,
                service_slug=None,
                plan="inspect dependencies",
                proposed_action="hold the rollout",
                action_approved=False,
                content="typed reflection rollback sentinel",
                metadata={},
                structured_payload={"schema_version": 1},
                provenance=Provenance(
                    "agent.reflect", decision_id, "verify atomic reflection persistence"
                ),
                parent_memory_ids=[],
            )
    finally:
        conn.rollback()
        conn.close()

    with connect(database_url()) as verifier:
        assert verifier.execute(
            "SELECT count(*) FROM semantic_memories WHERE producer_decision_id = %s",
            (decision_id,),
        ).fetchone() == (0,)
        assert verifier.execute(
            "SELECT count(*) FROM agent_reflections WHERE decision_id = %s",
            (decision_id,),
        ).fetchone() == (0,)
        assert verifier.execute(
            "SELECT status FROM memory_decisions WHERE id = %s", (decision_id,)
        ).fetchone() == ("open",)


@requires_db
def test_incident_graph_interrupt_resumes_from_cockroachdb_checkpoint():
    from hindsight.agent import IncidentInput, resume_incident_agent, run_incident_agent
    from hindsight.db import connect, database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import DeterministicReasoningProvider

    thread_id = f"agent-resume-{uuid4()}"
    incident = IncidentInput(
        user_input="search-api error rate spiked after the deploy",
        incident_id=f"incident-{uuid4()}",
        namespace=f"agent-resume-test-{uuid4()}",
        service_slug="search-api",
        severity="sev2",
        title="Search error spike",
    )
    first = run_incident_agent(
        incident,
        thread_id=thread_id,
        pause_before_act=True,
        reasoning_provider=DeterministicReasoningProvider(
            response_text="roll back the deploy candidate and verify error rate"
        ),
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert first.interrupted
    assert first.interrupt["thread_id"] == thread_id
    assert "proposed_action" in first.interrupt

    resumed = resume_incident_agent(
        thread_id=thread_id,
        approved=True,
        embedding_provider=DeterministicEmbeddingProvider(),
    )

    assert not resumed.interrupted
    assert resumed.state["action_approved"] is True
    assert resumed.reflected_memory_id is not None

    with connect(database_url()) as conn:
        checkpoint_rows = conn.execute(CHECKPOINT_QUERY.read_text(), (thread_id,)).fetchall()
        chat_rows = conn.execute(CHAT_QUERY.read_text(), (thread_id,)).fetchall()

    assert len(checkpoint_rows) >= 2
    assert [row[1] for row in chat_rows] == ["human", "ai"]
    assert "roll back the deploy candidate" in chat_rows[1][2]
