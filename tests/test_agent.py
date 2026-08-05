"""Tests for the CockroachDB-backed incident agent graph."""

import os
import pathlib
import asyncio
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set"
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
CHECKPOINT_QUERY = ROOT / "queries" / "agent_checkpoint_rows.sql"
CHAT_QUERY = ROOT / "queries" / "agent_chat_history.sql"


def _database_url(name: str, *, user: str | None = None) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    if user is None:
        return urlunsplit(parts._replace(path=f"/{name}"))
    host = parts.hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port is not None else ""
    return urlunsplit(
        parts._replace(
            netloc=f"{quote(user)}@{host}{port}",
            path=f"/{name}",
        )
    )


def _create_database(name: str) -> str:
    with psycopg.connect(_database_url("defaultdb"), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return _database_url(name)


def _drop_database(name: str) -> None:
    with psycopg.connect(_database_url("defaultdb"), autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(name)))


def test_async_sqlalchemy_url_uses_async_psycopg_driver():
    from hindsight.agent import _async_sqlalchemy_url

    assert _async_sqlalchemy_url("postgresql://root@localhost/db") == (
        "cockroachdb+psycopg://root@localhost/db"
    )
    assert _async_sqlalchemy_url("cockroachdb://root@localhost/db") == (
        "cockroachdb+psycopg://root@localhost/db"
    )


def test_reasoning_prompt_uses_typed_governance_envelopes():
    from hindsight.agent import _plan_prompt

    prompt = _plan_prompt(
        {
            "incident_id": "incident-1",
            "namespace": "namespace-1",
            "user_input": "checkout latency",
            "recalled_memories": [
                {
                    "id": "memory-1",
                    "belief_id": "belief-1",
                    "version_number": 2,
                    "content": "Throttle retries after inspecting the dependency.",
                    "content_schema": "semantic.v1",
                    "transition_kind": "supersession",
                    "trust_status": "active",
                    "writer": "operator",
                    "source_ref": "incident:resolved",
                    "justification": "Resolved incident evidence",
                    "metadata": {
                        "operator_disposition": "approved",
                        "safety_status": "safe",
                        "contradiction_status": "supported",
                        "evidence_quality": "resolved_incident",
                        "usage_instruction": "positive_guidance",
                    },
                }
            ],
        }
    )

    assert '"operator_disposition": "approved"' in prompt
    assert '"transition": "supersession"' in prompt
    assert '"usage_instruction": "positive_guidance"' in prompt
    assert '"evidence_quality": "resolved_incident"' in prompt


def test_signature_action_request_is_memory_causal_and_executes_only_when_called():
    from hindsight.agent import (
        _bounded_action_request,
        _execute_bounded_action,
        _guidance_eligible,
        _signature_actions,
    )
    from hindsight.simulator import DeterministicIncidentSimulator

    base = {
        "run_id": "run-1",
        "namespace": "live-browser:isolated-tenant",
        "incident_id": "demo-payments-checkout-latency:isolated-tenant",
    }
    corrected_state = {**base, "recalled_memories": []}
    poisoned_state = {
        **base,
        "recalled_memories": [{"metadata": {"role": "poison"}}],
    }
    corrected_actions = _signature_actions(corrected_state)
    poisoned_actions = _signature_actions(poisoned_state)
    unrelated_actions = _signature_actions(
        {
            **poisoned_state,
            "incident_id": "unrelated-checkout-latency",
        }
    )
    pending = _bounded_action_request(
        corrected_state,
        actions=corrected_actions,
        tool_name="deterministic_incident_simulator",
    )
    corrected = _execute_bounded_action(
        corrected_state,
        actions=corrected_actions,
        trace=pending,
        tool=DeterministicIncidentSimulator(),
    )
    poisoned = _execute_bounded_action(
        poisoned_state,
        actions=poisoned_actions,
        trace=_bounded_action_request(
            poisoned_state,
            actions=poisoned_actions,
            tool_name="deterministic_incident_simulator",
        ),
        tool=DeterministicIncidentSimulator(),
    )

    assert "score" not in pending
    assert corrected["request"]["actions"] == [
        "inspect_dependency",
        "throttle_retries",
    ]
    assert corrected["score"] == {"recovered": True, "unsafe_action_count": 0}
    assert poisoned["request"]["actions"] == ["scale_workers"]
    assert unrelated_actions == ()
    assert poisoned["score"] == {"recovered": False, "unsafe_action_count": 1}
    assert _guidance_eligible(action_approved=True, action_trace=corrected)
    assert not _guidance_eligible(action_approved=True, action_trace=poisoned)


def test_review_required_memory_cannot_claim_positive_guidance():
    from hindsight.agent import _governed_guidance_envelope

    envelope = _governed_guidance_envelope(
        {
            "id": "memory-rejected",
            "trust_status": "review_required",
            "metadata": {
                "operator_disposition": "rejected",
                "usage_instruction": "positive_guidance",
            },
        }
    )

    assert envelope["status"] == "review_required"
    assert envelope["usage_instruction"] == "audit_only"


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


def test_agent_storage_initializer_is_idempotent(monkeypatch):
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

    agent.setup_agent_storage(db_url="postgresql://db")
    agent.setup_agent_storage(db_url="postgresql://db")

    assert calls == {"checkpoint": 2, "chat": 2}


def test_agent_storage_validation_reports_missing_objects_without_ddl(monkeypatch):
    import hindsight.agent as agent
    from psycopg import errors

    statements = []

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            pass

        def execute(self, statement):
            statements.append(statement)
            if "FROM checkpoint_blobs" in statement:
                raise errors.UndefinedTable("checkpoint_blobs is missing")

    monkeypatch.setattr(agent, "connect", lambda *args, **kwargs: FakeConnection())

    with pytest.raises(
        agent.AgentStorageNotInitializedError,
        match="checkpoint_blobs is missing or incompatible",
    ):
        agent.validate_agent_storage(db_url="postgresql://db")

    assert statements
    assert all(
        not statement.lstrip().upper().startswith(("ALTER", "CREATE", "DROP"))
        for statement in statements
    )


@requires_db
def test_start_reports_uninitialized_storage_without_creating_it():
    from hindsight.agent import (
        AgentStorageNotInitializedError,
        IncidentInput,
        run_incident_agent,
    )
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import DeterministicReasoningProvider

    database_name = f"hindsight_agent_missing_{uuid4().hex}"
    target_url = _create_database(database_name)
    try:
        with pytest.raises(
            AgentStorageNotInitializedError,
            match="checkpoint_migrations is missing or incompatible",
        ):
            run_incident_agent(
                IncidentInput(user_input="latency", incident_id="incident-1"),
                db_url=target_url,
                reasoning_provider=DeterministicReasoningProvider(response_text="inspect"),
                embedding_provider=DeterministicEmbeddingProvider(),
            )

        with psycopg.connect(target_url) as conn:
            persistence_tables = conn.execute(
                """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name IN (
                          'agent_chat_messages', 'checkpoint_blobs',
                          'checkpoint_migrations', 'checkpoint_writes', 'checkpoints'
                      )
                """
            ).fetchall()
        assert persistence_tables == []
    finally:
        _drop_database(database_name)


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
        "action_tool": None,
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
        "action_tool": None,
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
        reflected_trust = conn.execute(
            "SELECT trust_status FROM semantic_memories WHERE id = %s",
            (resumed.reflected_memory_id,),
        ).fetchone()

    assert len(checkpoint_rows) >= 2
    assert [row[1] for row in chat_rows] == ["human", "ai"]
    assert "roll back the deploy candidate" in chat_rows[1][2]
    assert reflected_trust == ("active",)


@requires_db
def test_rejected_reflection_is_auditable_but_not_positive_retrieval():
    from hindsight.agent import IncidentInput, resume_incident_agent, run_incident_agent
    from hindsight.db import database_url
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore
    from hindsight.reasoning import DeterministicReasoningProvider

    thread_id = f"agent-rejected-{uuid4()}"
    namespace = f"demo:payments-poison-rewind:rejected:{uuid4()}"
    provider = DeterministicEmbeddingProvider()

    class RejectIfExecuted:
        name = "reject_if_executed"

        def execute(self, _request):
            raise AssertionError("rejected action must not execute")

    action_tool = RejectIfExecuted()
    first = run_incident_agent(
        IncidentInput(
            user_input="search-api error rate spiked after the deploy",
            incident_id=f"incident-{uuid4()}",
            namespace=namespace,
            service_slug="search-api",
        ),
        thread_id=thread_id,
        pause_before_act=True,
        reasoning_provider=DeterministicReasoningProvider(
            response_text="roll back the deploy candidate and verify error rate"
        ),
        embedding_provider=provider,
        action_tool=action_tool,
    )
    assert first.interrupted

    rejected = resume_incident_agent(
        thread_id=thread_id,
        approved=False,
        embedding_provider=provider,
        action_tool=action_tool,
    )
    memory_id = str(rejected.reflected_memory_id)
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        audit = store.audit_memory(memory_kind="semantic", memory_id=memory_id)
        result = store.retrieve_semantic(
            namespace=namespace,
            query="roll back deploy candidate",
            decision_id=f"rejected-retrieval:{uuid4()}",
            reader="pytest",
            purpose="prove rejected reflections are not positive guidance",
        )

    assert audit is not None
    assert audit["trust_status"] == "review_required"
    assert audit["structured_payload"]["action_approved"] is False
    assert rejected.state["action_trace"]["execution"]["status"] == "not_executed"
    assert "observations" not in rejected.state.get("action_trace", {})
    assert memory_id not in {str(hit["id"]) for hit in result.hits}


@requires_db
@pytest.mark.migration_acceptance
def test_preinitialized_agent_storage_supports_start_and_resume_without_create_privilege():
    from hindsight.agent import (
        IncidentInput,
        resume_incident_agent,
        run_incident_agent,
        setup_agent_storage,
    )
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.reasoning import DeterministicReasoningProvider

    database_name = f"hindsight_agent_runtime_{uuid4().hex}"
    role_name = f"agent_runtime_{uuid4().hex}"
    target_url = _create_database(database_name)
    try:
        with psycopg.connect(target_url, autocommit=True) as conn:
            for path in sorted((ROOT / "migrations").glob("[0-9]*.sql")):
                with conn.transaction():
                    conn.execute(path.read_text())

        setup_agent_storage(db_url=target_url)
        setup_agent_storage(db_url=target_url)

        with psycopg.connect(target_url, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role_name)))
            conn.execute((ROOT / "infra/db/roles.sql").read_text())
            conn.execute(
                sql.SQL("GRANT hindsight_memory_worker TO {}").format(
                    sql.Identifier(role_name)
                )
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(database_name),
                    sql.Identifier(role_name),
                )
            )

        runtime_url = _database_url(database_name, user=role_name)
        with psycopg.connect(runtime_url, autocommit=True) as runtime_conn:
            assert runtime_conn.execute("SELECT current_user").fetchone() == (role_name,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime_conn.execute("CREATE TABLE runtime_schema_change (id INT PRIMARY KEY)")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                runtime_conn.execute("DELETE FROM semantic_memories WHERE false")

        thread_id = f"restricted-role-{uuid4()}"
        first = run_incident_agent(
            IncidentInput(
                user_input="search-api error rate spiked after the deploy",
                incident_id=f"incident-{uuid4()}",
                namespace=f"restricted-role-{uuid4()}",
                service_slug="search-api",
                severity="sev2",
                title="Search error spike",
            ),
            thread_id=thread_id,
            pause_before_act=True,
            db_url=runtime_url,
            reasoning_provider=DeterministicReasoningProvider(
                response_text="roll back the deploy candidate and verify error rate"
            ),
            embedding_provider=DeterministicEmbeddingProvider(),
        )
        assert first.interrupted

        resumed = resume_incident_agent(
            thread_id=thread_id,
            approved=True,
            db_url=runtime_url,
            embedding_provider=DeterministicEmbeddingProvider(),
        )
        assert not resumed.interrupted
        assert resumed.reflected_memory_id is not None
    finally:
        _drop_database(database_name)
        with psycopg.connect(_database_url("defaultdb"), autocommit=True) as admin:
            admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role_name)))
