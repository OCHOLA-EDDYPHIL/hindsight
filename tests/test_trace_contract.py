"""Tests for the public governed-memory identity trace."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


@requires_db
def test_decision_trace_exposes_retrieval_profile_version_evidence_and_lineage():
    from hindsight.db import database_url
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance
    from hindsight.trace_contract import decision_influence, governed_decision_trace

    namespace = f"trace-contract:{uuid4()}"
    decision_id = f"trace-decision:{uuid4()}"
    provider = DeterministicEmbeddingProvider()
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        source = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="processor timeouts caused retry fanout",
            provenance=Provenance(
                "pytest.trace",
                "trace:source",
                "Seed a governed source memory",
            ),
        )
        second_source = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="processor timeouts caused retry fanout in payments",
            provenance=Provenance(
                "pytest.trace",
                "trace:second-source",
                "Seed a second governed source memory",
            ),
        )
        retrieval = store.retrieve_semantic(
            namespace=namespace,
            query="processor timeouts caused retry fanout",
            decision_id=decision_id,
            reader="pytest.trace",
            purpose="Build an inspectable decision trace",
            limit=2,
        )
        child = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="throttle retry fanout while processor timeouts remain high",
            provenance=Provenance(
                "pytest.trace",
                "trace:child",
                "Derive a child memory from the retrieved source",
            ),
            producer_decision_id=decision_id,
            parent_memory_ids=[str(source["id"]), str(second_source["id"])],
        )
        store.invalidate(
            memory_id=str(source["id"]),
            actor="pytest.trace",
            reason="Exercise invalidated trace rendering",
        )

    trace = governed_decision_trace(decision_id=decision_id, db_url=database_url())

    assert trace is not None
    assert trace["decision"]["id"] == decision_id
    assert str(trace["retrievals"][0]["id"]) == retrieval.retrieval_id
    assert trace["retrievals"][0]["embedding_profile_id"]
    assert trace["retrievals"][0]["embedding_provider"] == "test_deterministic"
    read = trace["reads"][0]
    assert str(read["memory_id"]) == str(source["id"])
    assert read["belief_id"] == source["belief_id"]
    assert read["version_number"] == source["version_number"]
    assert read["embedding_profile_id"] == trace["retrievals"][0]["embedding_profile_id"]
    assert read["memory_producer_decision_id"] == source["producer_decision_id"]
    assert read["memory_status"] == "invalidated"
    assert read["t_invalid"] is not None
    assert read["evidence_ids"]
    assert read["outgoing_lineage_edge_ids"]
    assert len(trace["lineage_edges"]) == 2
    assert {str(edge["child_semantic_memory_id"]) for edge in trace["lineage_edges"]} == {
        str(child["id"])
    }
    assert {edge["producer_decision_id"] for edge in trace["lineage_edges"]} == {decision_id}
    assert len({edge["created_at"] for edge in trace["lineage_edges"]}) == 1
    lineage_ids = [str(edge["id"]) for edge in trace["lineage_edges"]]
    assert lineage_ids == sorted(lineage_ids)

    repeated = governed_decision_trace(decision_id=decision_id, db_url=database_url())
    assert repeated is not None
    assert [str(edge["id"]) for edge in repeated["lineage_edges"]] == lineage_ids

    direct = decision_influence(decision_id=decision_id, db_url=database_url())
    assert direct["decision_id"] == decision_id
    assert direct["count"] == 2
    assert {row["memory"]["content"] for row in direct["memories"]} == {
        source["content"],
        second_source["content"],
    }
    assert direct["trace"]["reads"][0]["belief_id"] == source["belief_id"]

    from hindsight import api

    influence = api.decisions_influence(decision_id)
    assert influence["decision_id"] == decision_id
    assert influence["count"] == 2
    assert {row["memory"]["content"] for row in influence["memories"]} == {
        source["content"],
        second_source["content"],
    }
    assert influence["decision"]["id"] == decision_id
    assert influence["retrievals"][0]["id"] == retrieval.retrieval_id
    assert influence["trace"]["reads"][0]["belief_id"] == str(source["belief_id"])
    assert [str(edge["id"]) for edge in influence["trace"]["lineage_edges"]] == lineage_ids

    from fastapi.testclient import TestClient

    response = TestClient(api.app).get(f"/v1/decisions/{decision_id}/influence")
    assert response.status_code == 200
    assert [edge["id"] for edge in response.json()["trace"]["lineage_edges"]] == lineage_ids


@requires_db
def test_explicit_signature_scenario_returns_partial_identity_state():
    from hindsight.db import database_url
    from hindsight.demo_state import reset_poison_rewind_state
    from hindsight.trace_contract import signature_scenario_trace

    namespace = reset_poison_rewind_state(
        namespace=f"partial-signature:{uuid4()}",
        db_url=database_url(),
    )

    scenario = signature_scenario_trace(namespace=namespace, db_url=database_url())

    assert scenario is not None
    assert scenario["namespace"] == namespace
    assert scenario["incident"] is None
    assert scenario["runs"] == []
    assert scenario["operation"] is None
    assert scenario["stages"] == {
        "baseline_memory_id": None,
        "compromised_memory_id": None,
        "poison_memory_id": None,
        "influenced_decision_id": None,
        "rewind_operation_id": None,
        "corrected_decision_id": None,
    }


@requires_db
def test_signature_scenario_resolves_by_scenario_and_decision_identity():
    from hindsight.db import connect, database_url
    from hindsight.demo_state import (
        DEMO_INPUT,
        DEMO_NAMESPACE,
        ensure_poison_rewind_incident,
        poison_demo_memory,
        reset_poison_rewind_state,
        seed_good_demo_memory,
    )
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore
    from hindsight.runs import create_run
    from hindsight.trace_contract import signature_scenario_trace
    from psycopg.types.json import Jsonb

    provider = DeterministicEmbeddingProvider()
    fixture_id = uuid4()
    namespace = reset_poison_rewind_state(
        namespace=DEMO_NAMESPACE,
        session_id=fixture_id,
        db_url=database_url(),
    )
    incident = ensure_poison_rewind_incident(
        fixture_id=fixture_id,
        db_url=database_url(),
    )
    seed = seed_good_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )
    poison = poison_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )
    bad, _ = create_run(
        incident_slug=incident["slug"],
        namespace=namespace,
        user_input="poisoned run",
        db_url=database_url(),
    )
    corrected, _ = create_run(
        incident_slug=incident["slug"],
        namespace=namespace,
        user_input="corrected run",
        db_url=database_url(),
    )
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        bad_retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=DEMO_INPUT,
            decision_id=bad["decision_id"],
            reader="pytest.trace",
            purpose="Record the stale guidance that shaped the unsafe decision",
            positive_guidance_only=True,
        )
    assert str(poison["id"]) in {str(row["id"]) for row in bad_retrieval.hits}
    with connect(database_url()) as conn:
        with conn.transaction():
            conn.execute(
                """
                    UPDATE agent_runs
                    SET status = 'rejected', plan = 'rotate certificates',
                        action_approved = false, completed_at = now()
                    WHERE id = %s
                """,
                (bad["id"],),
            )
            conn.execute(
                """
                    UPDATE agent_runs
                    SET status = 'completed', plan = 'throttle retry fanout',
                        action_approved = true, completed_at = now()
                    WHERE id = %s
                """,
                (corrected["id"],),
            )
            for run, trace in (
                (
                    bad,
                    {
                        "request": {"id": "action:bad", "actions": ["scale_workers"]},
                        "score": {"recovered": False, "unsafe_action_count": 1},
                    },
                ),
                (
                    corrected,
                    {
                        "request": {
                            "id": "action:corrected",
                            "actions": ["inspect_dependency", "throttle_retries"],
                        },
                        "score": {"recovered": True, "unsafe_action_count": 0},
                    },
                ),
            ):
                conn.execute(
                    """
                        INSERT INTO agent_run_events (
                            run_id, sequence, phase, status, summary, metadata
                        )
                        SELECT %s, COALESCE(max(sequence), 0) + 1,
                               'completion', 'completed', 'Externally scored action', %s
                        FROM agent_run_events WHERE run_id = %s
                    """,
                    (run["id"], Jsonb({"action_trace": trace}), run["id"]),
                )
            operation = conn.execute(
                """
                    INSERT INTO memory_operations (
                        operation_type, actor, reason, namespace,
                        invalidated_memory_ids, restored_memory_ids,
                        idempotency_key, status, request_payload,
                        expected_revisions, applied_revisions, attempt_count,
                        completed_at
                    )
                    VALUES (
                        'rewind', 'pytest.trace', 'Remove poison', %s,
                        jsonb_build_array(%s::STRING), '[]'::JSONB, %s,
                        'completed', '{}'::JSONB,
                        '{}'::JSONB, '{}'::JSONB, 1, now()
                    )
                    RETURNING id
                """,
                (namespace, str(poison["id"]), f"trace:{uuid4()}"),
            ).fetchone()[0]
            conn.execute(
                """
                    INSERT INTO memory_operation_events (
                        operation_id, sequence, status, summary
                    ) VALUES (%s, 1, 'completed', 'Memory operation completed')
                """,
                (operation,),
            )
            conn.execute(
                """
                    INSERT INTO memory_operation_effects (
                        operation_id, sequence, effect_type,
                        source_memory_id, belief_id, namespace
                    ) VALUES (%s, 1, 'closed', %s, %s, %s)
                """,
                (operation, poison["id"], poison["belief_id"], namespace),
            )
    with MemoryStore(url=database_url()) as store:
        store.invalidate(
            memory_id=str(poison["id"]),
            actor="pytest.trace",
            reason="Remove poison",
        )
    with MemoryStore(url=database_url(), embedding_provider=provider) as store:
        corrected_retrieval = store.retrieve_semantic(
            namespace=namespace,
            query=DEMO_INPUT,
            decision_id=corrected["decision_id"],
            reader="pytest.trace",
            purpose="Record the corrected decision after rewind",
            positive_guidance_only=True,
        )
    assert str(poison["id"]) not in {str(row["id"]) for row in corrected_retrieval.hits}

    validation_namespace = reset_poison_rewind_state(
        namespace=f"live-browser:{uuid4()}",
        db_url=database_url(),
    )
    validation_bad, _ = create_run(
        incident_slug=incident["slug"],
        namespace=validation_namespace,
        user_input="validation fixture rejected run",
        db_url=database_url(),
    )
    validation_corrected, _ = create_run(
        incident_slug=incident["slug"],
        namespace=validation_namespace,
        user_input="validation fixture corrected run",
        db_url=database_url(),
    )
    with connect(database_url()) as conn:
        with conn.transaction():
            conn.execute(
                """
                    UPDATE agent_runs SET status = 'rejected', completed_at = now()
                    WHERE id = %s
                """,
                (validation_bad["id"],),
            )
            conn.execute(
                """
                    UPDATE agent_runs SET status = 'completed', completed_at = now()
                    WHERE id = %s
                """,
                (validation_corrected["id"],),
            )
            conn.execute(
                """
                    INSERT INTO memory_operations (
                        operation_type, actor, reason, namespace,
                        invalidated_memory_ids, restored_memory_ids,
                        idempotency_key, status, request_payload,
                        expected_revisions, applied_revisions, attempt_count,
                        completed_at
                    )
                    VALUES (
                        'rewind', 'pytest.trace', 'Validation fixture', %s,
                        '[]'::JSONB, '[]'::JSONB, %s,
                        'completed', '{}'::JSONB,
                        '{}'::JSONB, '{}'::JSONB, 1, now()
                    )
                """,
                (validation_namespace, f"trace:{uuid4()}"),
            )

    default = signature_scenario_trace(db_url=database_url())
    assert default is not None
    assert default["namespace"] == namespace
    assert default["incident"]["slug"] == incident["slug"]
    assert default["stages"]["baseline_memory_id"] == seed["id"]
    assert default["stages"]["compromised_memory_id"] == poison["id"]
    assert default["stages"]["poison_memory_id"] == poison["id"]
    assert default["stages"]["influenced_decision_id"] == bad["decision_id"]
    assert default["stages"]["rewind_operation_id"] == operation
    assert default["stages"]["corrected_decision_id"] == corrected["decision_id"]
    bad_trace = next(run for run in default["runs"] if str(run["id"]) == bad["id"])
    corrected_trace = next(run for run in default["runs"] if str(run["id"]) == corrected["id"])
    assert bad_trace["action_trace"]["score"] == {
        "recovered": False,
        "unsafe_action_count": 1,
    }
    poison_read = next(
        read for read in bad_trace["trace"]["reads"] if str(read["memory_id"]) == str(poison["id"])
    )
    assert poison_read["writer"] == "demo.fixture-import"
    assert poison_read["source_ref"] == "demo:stale-runbook-import"
    assert "previously approved payment runbook" in poison_read["justification"]
    assert corrected_trace["action_trace"]["score"] == {
        "recovered": True,
        "unsafe_action_count": 0,
    }
    assert (
        next(row for row in default["memories"] if row["id"] == poison["id"])["t_invalid"]
        is not None
    )
    by_scenario = signature_scenario_trace(
        scenario_id=str(default["scenario_id"]),
        db_url=database_url(),
    )
    by_decision = signature_scenario_trace(
        decision_id=corrected["decision_id"],
        db_url=database_url(),
    )
    validation_by_decision = signature_scenario_trace(
        decision_id=validation_corrected["decision_id"],
        db_url=database_url(),
    )
    assert by_scenario is not None and by_scenario["namespace"] == namespace
    assert by_decision is not None and by_decision["namespace"] == namespace
    assert validation_by_decision is not None
    assert validation_by_decision["namespace"] == validation_namespace

    from fastapi.testclient import TestClient
    from hindsight.api import app

    client = TestClient(app)
    public = client.get(
        "/v1/signature-scenarios",
        params={"decision_id": corrected["decision_id"]},
    )
    assert public.status_code == 200
    assert public.json()["scenario_id"] == str(default["scenario_id"])
    assert "content" not in public.json()["memories"][0]
    public_poison_read = next(
        read
        for run in public.json()["runs"]
        for read in (run.get("trace") or {}).get("reads", [])
        if read.get("writer") == "demo.fixture-import"
    )
    assert public_poison_read["source_ref"] == "demo:stale-runbook-import"
    assert "previously approved payment runbook" in public_poison_read["justification"]
    deep_link = client.get(f"/v1/signature-scenarios/{default['scenario_id']}")
    assert deep_link.status_code == 200
    assert deep_link.json()["namespace"] == namespace


def test_trace_selectors_are_mutually_exclusive():
    from hindsight.trace_contract import signature_scenario_trace

    with pytest.raises(ValueError, match="only one"):
        signature_scenario_trace(scenario_id="one", decision_id="two")


@requires_db
def test_missing_trace_identities_return_no_trace():
    from hindsight.trace_contract import (
        governed_decision_trace,
        signature_scenario_trace,
    )

    assert signature_scenario_trace(scenario_id="not-a-uuid") is None
    assert governed_decision_trace(decision_id="missing-decision") is None
