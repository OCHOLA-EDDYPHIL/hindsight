"""Tests for the public governed-memory identity trace."""

import os
from uuid import uuid4

import pytest

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def test_public_redaction_removes_nested_aws_account_identifiers():
    from hindsight.redaction import redact_account_identifiers

    secret = "123456789012"
    redacted = redact_account_identifiers(
        {
            "account_id": secret,
            "nested": [{"aws_account_id": secret, "region": "us-east-1"}],
        }
    )

    assert redacted == {"nested": [{"region": "us-east-1"}]}
    assert secret not in str(redacted)


def test_signature_trace_pairs_latest_pre_rewind_rejection_with_correction():
    from datetime import UTC, datetime, timedelta

    from hindsight.trace_contract import _rejected_run_for_operation

    rewind_completed_at = datetime.now(UTC)
    oldest = {
        "id": "oldest-rejection",
        "status": "rejected",
        "completed_at": rewind_completed_at - timedelta(minutes=3),
    }
    corrected_rejection = {
        "id": "corrected-rejection",
        "status": "rejected",
        "completed_at": rewind_completed_at - timedelta(minutes=1),
    }
    later_rejection = {
        "id": "later-rejection",
        "status": "rejected",
        "completed_at": rewind_completed_at + timedelta(minutes=1),
    }

    selected = _rejected_run_for_operation(
        runs=[oldest, corrected_rejection, later_rejection],
        operation={"completed_at": rewind_completed_at},
    )

    assert selected == corrected_rejection


def test_action_comparison_requires_structured_actions_equivalent_context_and_lineage():
    from copy import deepcopy

    from hindsight.agent_decision import operational_action_fingerprint
    from hindsight.trace_contract import _action_comparison

    prompt = (
        "Checkout p99 is above 2s and the queue is growing. Inspect current telemetry "
        "and recommend one reversible next action."
    )

    def observation(timestamp: str, *, account_id: str) -> dict:
        return {
            "status": "available",
            "tool": "aws_cloudwatch_diagnostics",
            "query_key": "payments.checkout_latency_ms",
            "account_id": account_id,
            "region": "us-east-1",
            "metric": {
                "namespace": "Hindsight/Demo",
                "name": "CheckoutLatency",
                "dimensions": [
                    {"name": "Service", "value": "payments-api"},
                    {"name": "Stage", "value": "demo"},
                ],
                "statistic": "Maximum",
                "period_seconds": 60,
            },
            "window": {"start": timestamp, "end": timestamp, "seconds": 900},
            "datapoints": [{"timestamp": timestamp, "value": 2400.0}],
            "datapoint_count": 1,
        }

    def run(decision_id: str, memory_id: str, action: str, timestamp: str) -> dict:
        payload = {
            "contract": "payments_retry_amplification.v1",
            "primary_action": action,
        }
        return {
            "decision_id": decision_id,
            "user_input": prompt,
            "trace": {"reads": [{"memory_id": memory_id}]},
            "action_trace": {
                "observations": [observation(timestamp, account_id=f"secret-{decision_id}")],
                "recommendation": {
                    "operational_action": {
                        **payload,
                        "fingerprint": operational_action_fingerprint(payload),
                    }
                },
            },
        }

    seed = {
        "id": "memory-v1",
        "belief_id": "belief-1",
        "version_number": 1,
        "transition_kind": "assertion",
        "t_invalid": "2026-08-13T10:01:00Z",
    }
    stale = {
        "id": "memory-v2",
        "belief_id": "belief-1",
        "version_number": 2,
        "previous_version_id": "memory-v1",
        "transition_kind": "supersession",
        "t_invalid": "2026-08-13T10:04:00Z",
    }
    restored = {
        "id": "memory-v3",
        "belief_id": "belief-1",
        "version_number": 3,
        "previous_version_id": "memory-v2",
        "transition_kind": "rewind_reassertion",
        "created_by_operation_id": "operation-1",
        "t_invalid": None,
    }
    rejected = run("decision-before", "memory-v2", "scale_workers", "2026-08-13T10:02:00Z")
    corrected = run(
        "decision-after",
        "memory-v3",
        "throttle_retries",
        "2026-08-13T10:05:00Z",
    )
    operation = {
        "id": "operation-1",
        "status": "completed",
        "invalidated_memory_ids": ["memory-v2"],
    }
    effects = [
        {
            "effect_type": "reasserted",
            "source_memory_id": "memory-v1",
            "result_memory_id": "memory-v3",
            "belief_id": "belief-1",
        }
    ]

    comparison = _action_comparison(
        rejected=rejected,
        corrected=corrected,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )

    assert comparison["status"] == "changed"
    assert comparison["before"]["primary_action"] == "scale_workers"
    assert comparison["after"]["primary_action"] == "throttle_retries"
    assert comparison["context"] == {
        "prompt_equal": True,
        "normalized_telemetry_equal": True,
    }
    assert comparison["memory_correction_proven"] is True
    assert comparison["controlled_pair"] is True

    different_prompt = deepcopy(corrected)
    different_prompt["user_input"] = "A changed report"
    not_controlled = _action_comparison(
        rejected=rejected,
        corrected=different_prompt,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert not_controlled["status"] == "changed"
    assert not_controlled["controlled_pair"] is False

    tampered = deepcopy(corrected)
    tampered["action_trace"]["recommendation"]["operational_action"]["fingerprint"] = (
        "operational_action:tampered"
    )
    unavailable = _action_comparison(
        rejected=rejected,
        corrected=tampered,
        operation=operation,
        operation_effects=effects,
        memories=[seed, stale, restored],
        seed=seed,
        compromised=stale,
    )
    assert unavailable["status"] == "unavailable"
    assert unavailable["controlled_pair"] is False


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
    from hindsight.demo_state import (
        ensure_poison_rewind_incident,
        record_poison_rewind_anchor,
        reset_poison_rewind_state,
    )
    from hindsight.trace_contract import signature_scenario_trace

    fixture_id = uuid4()
    incident = ensure_poison_rewind_incident(
        fixture_id=fixture_id,
        db_url=database_url(),
    )
    namespace = reset_poison_rewind_state(
        namespace=f"partial-signature:{uuid4()}",
        session_id=fixture_id,
        incident_id=fixture_id,
        db_url=database_url(),
    )
    rewind_anchor = record_poison_rewind_anchor(
        namespace=namespace,
        db_url=database_url(),
    )

    scenario = signature_scenario_trace(namespace=namespace, db_url=database_url())

    assert scenario is not None
    assert scenario["scenario_id"] == fixture_id
    assert scenario["namespace"] == namespace
    assert scenario["status"] == "active"
    assert scenario["session_status"] == "active"
    assert scenario["completed_at"] is None
    assert scenario["rewind_anchor"] == rewind_anchor
    assert scenario["incident"]["id"] == fixture_id
    assert scenario["incident"]["slug"] == incident["slug"]
    assert scenario["incident"]["service_slug"] == "payments-api"
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
        record_poison_rewind_anchor,
        reset_poison_rewind_state,
        seed_good_demo_memory,
    )
    from tests.fakes import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore
    from hindsight.operations import enqueue_operation, execute_operation, preview_rewind
    from hindsight.runs import create_run
    from hindsight.trace_contract import signature_scenario_trace
    from psycopg.types.json import Jsonb

    provider = DeterministicEmbeddingProvider()
    fixture_id = uuid4()
    incident = ensure_poison_rewind_incident(
        fixture_id=fixture_id,
        db_url=database_url(),
    )
    namespace = reset_poison_rewind_state(
        namespace=DEMO_NAMESPACE,
        session_id=fixture_id,
        incident_id=fixture_id,
        db_url=database_url(),
    )
    seed = seed_good_demo_memory(
        namespace=namespace,
        db_url=database_url(),
        embedding_provider=provider,
    )
    rewind_anchor = record_poison_rewind_anchor(
        namespace=namespace,
        db_url=database_url(),
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
                    INSERT INTO agent_run_events (
                        run_id, sequence, phase, status, summary, metadata
                    )
                    SELECT %s, COALESCE(max(sequence), 0) + 1,
                           'completion', 'rejected', 'Operator rejected recommendation', %s
                    FROM agent_run_events WHERE run_id = %s
                """,
                (
                    bad["id"],
                    Jsonb(
                        {
                            "action_trace": {
                                "mode": "recommendation_only",
                                "approval": {"approved": False, "disposition": "rejected"},
                                "execution": {"status": "not_executed"},
                            }
                        }
                    ),
                    bad["id"],
                ),
            )
    preview = preview_rewind(
        namespace=namespace,
        target_timestamp=rewind_anchor,
        actor="pytest.trace",
        reason="Restore the accepted belief version",
        db_url=database_url(),
    )
    queued, _ = enqueue_operation(
        preview_id=str(preview["id"]),
        fingerprint=str(preview["fingerprint"]),
        idempotency_key=f"trace:{uuid4()}",
        db_url=database_url(),
    )
    completed_operation = execute_operation(
        operation_id=str(queued["id"]),
        embedding_provider=provider,
        worker_id="pytest.trace",
        db_url=database_url(),
    )
    operation = completed_operation["id"]
    corrected, _ = create_run(
        incident_slug=incident["slug"],
        namespace=namespace,
        user_input="corrected run",
        db_url=database_url(),
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
    with connect(database_url()) as conn:
        with conn.transaction():
            conn.execute(
                """
                    UPDATE agent_runs
                    SET status = 'completed', plan = 'throttle retry fanout',
                        action_approved = true, completed_at = now()
                    WHERE id = %s
                """,
                (corrected["id"],),
            )
            conn.execute(
                """
                    INSERT INTO agent_run_events (
                        run_id, sequence, phase, status, summary, metadata
                    )
                    SELECT %s, COALESCE(max(sequence), 0) + 1,
                           'completion', 'completed', 'Recommendation approved', %s
                    FROM agent_run_events WHERE run_id = %s
                """,
                (
                    corrected["id"],
                    Jsonb(
                        {
                            "action_trace": {
                                "mode": "recommendation_only",
                                "approval": {"approved": True, "disposition": "approved"},
                                "execution": {"status": "recommendation_approved"},
                            }
                        }
                    ),
                    corrected["id"],
                ),
            )

    validation_namespace = reset_poison_rewind_state(
        namespace=f"{DEMO_NAMESPACE}:session:{uuid4().hex}",
        incident_id=fixture_id,
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
                    UPDATE agent_runs
                    SET status = 'completed', action_approved = true,
                        completed_at = now()
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
    assert default["scenario_id"] == fixture_id
    assert default["namespace"] == namespace
    assert default["status"] == "completed"
    assert default["session_status"] == "active"
    assert default["completed_at"] is not None
    assert default["rewind_anchor"] == rewind_anchor
    assert default["incident"]["slug"] == incident["slug"]
    assert default["incident"]["service_slug"] == "payments-api"
    assert default["stages"]["baseline_memory_id"] == seed["id"]
    assert default["stages"]["compromised_memory_id"] == poison["id"]
    assert default["stages"]["poison_memory_id"] == poison["id"]
    assert default["stages"]["influenced_decision_id"] == bad["decision_id"]
    assert default["stages"]["rewind_operation_id"] == operation
    assert default["stages"]["corrected_decision_id"] == corrected["decision_id"]
    bad_trace = next(run for run in default["runs"] if str(run["id"]) == bad["id"])
    corrected_trace = next(run for run in default["runs"] if str(run["id"]) == corrected["id"])
    assert bad_trace["action_trace"]["mode"] == "recommendation_only"
    assert bad_trace["action_trace"]["approval"]["approved"] is False
    assert bad_trace["action_trace"]["execution"]["status"] == "not_executed"
    poison_read = next(
        read for read in bad_trace["trace"]["reads"] if str(read["memory_id"]) == str(poison["id"])
    )
    assert poison_read["writer"] == "demo.fixture-import"
    assert poison_read["source_ref"] == "demo:stale-runbook-import"
    assert "previously approved payment runbook" in poison_read["justification"]
    assert corrected_trace["action_trace"]["mode"] == "recommendation_only"
    assert corrected_trace["action_trace"]["approval"]["approved"] is True
    assert corrected_trace["action_trace"]["execution"]["status"] == "recommendation_approved"
    assert corrected_trace["created_at"] > default["operation"]["completed_at"]
    assert corrected_trace["trace"]["reads"]
    assert all(
        str(read["memory_id"])
        not in {str(value) for value in default["operation"]["invalidated_memory_ids"]}
        for read in corrected_trace["trace"]["reads"]
    )
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
    assert validation_by_decision["status"] == "active"
    assert validation_by_decision["stages"]["corrected_decision_id"] is None

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
