"""Tests for the CockroachDB-backed incident agent graph."""

import os
import pathlib
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

    assert checkpoint_rows
    assert {row[1] for row in chat_rows} == {"human", "ai"}
    assert "checkout latency" in chat_rows[0][2]


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
