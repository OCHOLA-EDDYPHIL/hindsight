"""Read-only identity traces for governed decisions and signature scenarios."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from psycopg.rows import dict_row

from hindsight.db import connect
from hindsight.demo_state import DEMO_NAMESPACE


def governed_decision_trace(
    *, decision_id: str, db_url: str | None = None
) -> dict[str, Any] | None:
    """Return the durable identities connecting one decision to governed memory."""

    with connect(db_url, application_name="hindsight-trace-api") as conn:
        return _governed_decision_trace(conn, decision_id=decision_id)


def lesson_identity_trace(
    *, decision_id: str, db_url: str | None = None
) -> dict[str, Any] | None:
    """Return one content-free identity chain for a retrieved procedural lesson."""

    with connect(db_url, application_name="hindsight-lesson-trace") as conn:
        return _lesson_identity_trace(conn, decision_id=decision_id)


def lesson_identity_traces(
    *, db_url: str | None = None, limit: int = 10
) -> list[dict[str, Any]]:
    """Return recent content-free procedural-lesson identity chains."""

    with connect(db_url, application_name="hindsight-lesson-traces") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT read.decision_id, max(read.read_at) AS latest_read
                    FROM memory_reads AS read
                    JOIN semantic_memories AS lesson
                        ON lesson.id = read.semantic_memory_id
                    WHERE lesson.content_schema = 'procedural_lesson.v1'
                      AND read.retrieval_id IS NOT NULL
                    GROUP BY read.decision_id
                    ORDER BY latest_read DESC
                    LIMIT %s
                """,
                (limit,),
            )
            decision_ids = [str(row["decision_id"]) for row in cur.fetchall()]
        return [
            trace
            for decision_id in decision_ids
            if (trace := _lesson_identity_trace(conn, decision_id=decision_id))
            is not None
        ]


def signature_scenario_trace(
    *,
    scenario_id: str | None = None,
    decision_id: str | None = None,
    namespace: str | None = None,
    db_url: str | None = None,
) -> dict[str, Any] | None:
    """Resolve one poison, rewind, and correction scenario without exposing memory content."""

    selectors = [value for value in (scenario_id, decision_id, namespace) if value]
    if len(selectors) > 1:
        raise ValueError("provide only one scenario selector")

    with connect(db_url, application_name="hindsight-signature-trace") as conn:
        session = _signature_session(
            conn,
            scenario_id=scenario_id,
            decision_id=decision_id,
            namespace=namespace,
        )
        if session is None:
            return None

        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT incident.*
                    FROM incidents AS incident
                    JOIN agent_runs AS run ON run.incident_id = incident.id
                    WHERE run.namespace = %s
                    ORDER BY run.created_at
                    LIMIT 1
                """,
                (session["namespace"],),
            )
            incident = cur.fetchone()
            cur.execute(
                """
                    SELECT id, thread_id, incident_id, incident_slug, namespace,
                           service_slug, status, decision_id, plan, proposed_action,
                           action_approved, provider, model, reflected_memory_id,
                           failure_code, created_at, started_at, updated_at, completed_at
                    FROM agent_runs
                    WHERE namespace = %s
                    ORDER BY created_at
                """,
                (session["namespace"],),
            )
            runs = [dict(row) for row in cur.fetchall()]
            run_events: dict[str, list[dict[str, Any]]] = {}
            if runs:
                cur.execute(
                    """
                        SELECT run_id, sequence, phase, status, summary, metadata, created_at
                        FROM agent_run_events
                        WHERE run_id = ANY(%s)
                        ORDER BY run_id, sequence
                    """,
                    ([run["id"] for run in runs],),
                )
                for event in cur.fetchall():
                    run_events.setdefault(str(event["run_id"]), []).append(dict(event))
            cur.execute(
                """
                    SELECT id, operation_type, actor, reason, target_timestamp,
                           namespace, invalidated_memory_ids, restored_memory_ids,
                           status, attempt_count, created_at, completed_at,
                           failure_code
                    FROM memory_operations
                    WHERE namespace = %s AND operation_type = 'rewind'
                    ORDER BY created_at DESC
                    LIMIT 1
                """,
                (session["namespace"],),
            )
            operation = cur.fetchone()
            events: list[dict[str, Any]] = []
            effects: list[dict[str, Any]] = []
            if operation is not None:
                cur.execute(
                    """
                        SELECT id, operation_id, sequence, status, summary, created_at
                        FROM memory_operation_events
                        WHERE operation_id = %s
                        ORDER BY sequence
                    """,
                    (operation["id"],),
                )
                events = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                        SELECT operation_id, sequence, effect_type, source_memory_id,
                               result_memory_id, belief_id, namespace, created_at
                        FROM memory_operation_effects
                        WHERE operation_id = %s
                        ORDER BY sequence
                    """,
                    (operation["id"],),
                )
                effects = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                    SELECT id, namespace, belief_id, version_number,
                           previous_version_id, producer_decision_id, transition_kind,
                           content_schema, lineage_status, trust_status,
                           created_by_operation_id, writer, t_valid, t_invalid,
                           written_at, invalidated_at
                    FROM semantic_memories
                    WHERE namespace = %s
                    ORDER BY t_valid, written_at
                """,
                (session["namespace"],),
            )
            memories = [dict(row) for row in cur.fetchall()]

        for run in runs:
            run["trace"] = _governed_decision_trace(conn, decision_id=run["decision_id"])
            run["events"] = run_events.get(str(run["id"]), [])
            run["action_trace"] = next(
                (
                    event["metadata"]["action_trace"]
                    for event in reversed(run["events"])
                    if isinstance(event.get("metadata"), dict)
                    and event["metadata"].get("action_trace")
                ),
                None,
            )

        rejected = next((run for run in runs if run["status"] == "rejected"), None)
        corrected = next(
            (run for run in reversed(runs) if run["status"] == "completed"),
            None,
        )
        seed = next((row for row in memories if row["writer"] == "demo.seed"), None)
        poison = next((row for row in memories if row["writer"] == "demo.poison"), None)
        stages = {
            "baseline_memory_id": seed["id"] if seed else None,
            "poison_memory_id": poison["id"] if poison else None,
            "influenced_decision_id": rejected["decision_id"] if rejected else None,
            "rewind_operation_id": operation["id"] if operation else None,
            "corrected_decision_id": corrected["decision_id"] if corrected else None,
        }
        return {
            "scenario_id": session["id"],
            "namespace": session["namespace"],
            "status": session["status"],
            "created_at": session["created_at"],
            "incident": dict(incident) if incident is not None else None,
            "runs": runs,
            "operation": dict(operation) if operation is not None else None,
            "operation_events": events,
            "operation_effects": effects,
            "memories": memories,
            "stages": stages,
        }


def _signature_session(
    conn: Any,
    *,
    scenario_id: str | None,
    decision_id: str | None,
    namespace: str | None,
) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        if scenario_id:
            try:
                resolved_scenario_id = UUID(scenario_id)
            except ValueError:
                return None
            cur.execute(
                """
                    SELECT id, namespace, status, created_at
                    FROM demo_sessions
                    WHERE id = %s AND demo_kind = 'poison_rewind'
                """,
                (resolved_scenario_id,),
            )
        elif decision_id:
            cur.execute(
                """
                    SELECT session.id, session.namespace, session.status,
                           session.created_at
                    FROM agent_runs AS run
                    JOIN demo_sessions AS session ON session.namespace = run.namespace
                    WHERE run.decision_id = %s
                      AND session.demo_kind = 'poison_rewind'
                """,
                (decision_id,),
            )
        elif namespace:
            cur.execute(
                """
                    SELECT id, namespace, status, created_at
                    FROM demo_sessions
                    WHERE namespace = %s AND demo_kind = 'poison_rewind'
                """,
                (namespace,),
            )
        else:
            cur.execute(
                """
                    SELECT session.id, session.namespace, session.status,
                           session.created_at
                    FROM demo_sessions AS session
                    WHERE session.demo_kind = 'poison_rewind'
                      AND session.created_by = 'dashboard.operator'
                      AND session.namespace LIKE %s
                      AND EXISTS (
                          SELECT 1 FROM agent_runs AS run
                          WHERE run.namespace = session.namespace
                            AND run.status = 'rejected'
                      )
                      AND EXISTS (
                          SELECT 1 FROM agent_runs AS run
                          WHERE run.namespace = session.namespace
                            AND run.status = 'completed'
                      )
                      AND EXISTS (
                          SELECT 1 FROM memory_operations AS operation
                          WHERE operation.namespace = session.namespace
                            AND operation.operation_type = 'rewind'
                            AND operation.status = 'completed'
                      )
                    ORDER BY session.created_at DESC
                    LIMIT 1
                """,
                (f"{DEMO_NAMESPACE}:session:%",),
            )
        row = cur.fetchone()
    return dict(row) if row is not None else None


def _governed_decision_trace(conn: Any, *, decision_id: str) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT id, actor, decision_kind, purpose, run_id, namespace,
                       status, opened_at, sealed_at
                FROM memory_decisions
                WHERE id = %s
            """,
            (decision_id,),
        )
        decision = cur.fetchone()
        if decision is None:
            return None

        cur.execute(
            """
                SELECT retrieval.id, retrieval.decision_id, retrieval.namespace,
                       retrieval.reader, retrieval.purpose, retrieval.policy,
                       retrieval.policy_version, retrieval.requested_limit,
                       retrieval.status, retrieval.selected_strategy,
                       retrieval.fallback_reason, retrieval.embedding_profile_id,
                       retrieval.returned_memory_ids, retrieval.error_code,
                       retrieval.started_at, retrieval.completed_at,
                       profile.provider AS embedding_provider,
                       profile.model AS embedding_model,
                       profile.dimensions AS embedding_dimensions,
                       profile.capability AS embedding_capability,
                       profile.encoder_revision,
                       profile.max_distance
                FROM memory_retrievals AS retrieval
                LEFT JOIN embedding_profiles AS profile
                    ON profile.id = retrieval.embedding_profile_id
                WHERE retrieval.decision_id = %s
                ORDER BY retrieval.started_at
            """,
            (decision_id,),
        )
        retrievals = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
                SELECT read.id, read.decision_id, read.memory_kind,
                       read.memory_id, read.reader, read.purpose, read.read_at,
                       read.retrieval_id, read.rank, read.distance,
                       semantic.belief_id, semantic.version_number,
                       semantic.previous_version_id,
                       semantic.producer_decision_id,
                       semantic.transition_kind, semantic.content_schema,
                       semantic.lineage_status, semantic.trust_status,
                       semantic.created_by_operation_id,
                       COALESCE(semantic.producer_decision_id,
                                episodic.producer_decision_id) AS memory_producer_decision_id,
                       COALESCE(semantic.content_schema,
                                episodic.content_schema) AS memory_content_schema,
                       COALESCE(semantic.lineage_status,
                                episodic.lineage_status) AS memory_lineage_status,
                       COALESCE(semantic.trust_status,
                                episodic.trust_status) AS memory_trust_status,
                       COALESCE(semantic.t_valid, episodic.t_valid) AS t_valid,
                       COALESCE(semantic.t_invalid, episodic.t_invalid) AS t_invalid,
                       CASE
                           WHEN COALESCE(semantic.t_invalid, episodic.t_invalid) IS NULL
                           THEN 'current'
                           ELSE 'invalidated'
                       END AS memory_status
                FROM memory_reads AS read
                LEFT JOIN semantic_memories AS semantic
                    ON read.semantic_memory_id = semantic.id
                LEFT JOIN episodic_memories AS episodic
                    ON read.episodic_memory_id = episodic.id
                WHERE read.decision_id = %s
                ORDER BY read.read_at, read.rank
            """,
            (decision_id,),
        )
        reads = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
                SELECT evidence.id, evidence.semantic_memory_id,
                       evidence.episodic_memory_id, evidence.evidence_kind,
                       evidence.evidence_digest, evidence.observed_at,
                       evidence.actor, evidence.created_at
                FROM memory_external_evidence AS evidence
                WHERE evidence.semantic_memory_id IN (
                    SELECT semantic_memory_id FROM memory_reads
                    WHERE decision_id = %s AND semantic_memory_id IS NOT NULL
                ) OR evidence.episodic_memory_id IN (
                    SELECT episodic_memory_id FROM memory_reads
                    WHERE decision_id = %s AND episodic_memory_id IS NOT NULL
                )
                ORDER BY evidence.created_at
            """,
            (decision_id, decision_id),
        )
        evidence = [dict(row) for row in cur.fetchall()]
        cur.execute(
            """
                SELECT edge.id, edge.child_semantic_memory_id,
                       edge.child_episodic_memory_id, edge.parent_read_id,
                       edge.producer_decision_id, edge.edge_type,
                       edge.created_at, parent.memory_kind AS parent_memory_kind,
                       parent.memory_id AS parent_memory_id,
                       parent.retrieval_id AS parent_retrieval_id
                FROM memory_lineage_edges AS edge
                JOIN memory_reads AS parent ON parent.id = edge.parent_read_id
                WHERE edge.parent_read_id IN (
                    SELECT id FROM memory_reads WHERE decision_id = %s
                ) OR edge.child_semantic_memory_id IN (
                    SELECT semantic_memory_id FROM memory_reads
                    WHERE decision_id = %s AND semantic_memory_id IS NOT NULL
                ) OR edge.child_episodic_memory_id IN (
                    SELECT episodic_memory_id FROM memory_reads
                    WHERE decision_id = %s AND episodic_memory_id IS NOT NULL
                )
                ORDER BY edge.created_at, edge.id
            """,
            (decision_id, decision_id, decision_id),
        )
        lineage = [dict(row) for row in cur.fetchall()]

    retrieval_profiles = {
        str(row["id"]): row.get("embedding_profile_id") for row in retrievals
    }
    for read in reads:
        memory_id = str(read["memory_id"])
        read_id = str(read["id"])
        read["embedding_profile_id"] = retrieval_profiles.get(
            str(read["retrieval_id"])
        )
        read["evidence_ids"] = [
            row["id"]
            for row in evidence
            if str(row.get("semantic_memory_id") or row.get("episodic_memory_id"))
            == memory_id
        ]
        read["incoming_lineage_edge_ids"] = [
            row["id"]
            for row in lineage
            if str(
                row.get("child_semantic_memory_id")
                or row.get("child_episodic_memory_id")
            )
            == memory_id
        ]
        read["outgoing_lineage_edge_ids"] = [
            row["id"] for row in lineage if str(row["parent_read_id"]) == read_id
        ]
    return {
        "decision": dict(decision),
        "retrievals": retrievals,
        "reads": reads,
        "evidence": evidence,
        "lineage_edges": lineage,
    }


def _lesson_identity_trace(conn: Any, *, decision_id: str) -> dict[str, Any] | None:
    decision_trace = _governed_decision_trace(conn, decision_id=decision_id)
    if decision_trace is None:
        return None
    lesson_read = next(
        (
            row
            for row in decision_trace["reads"]
            if row.get("memory_content_schema") == "procedural_lesson.v1"
            and row.get("retrieval_id") is not None
        ),
        None,
    )
    if lesson_read is None:
        return None

    lesson_memory_id = str(lesson_read["memory_id"])
    retrieval = next(
        (
            row
            for row in decision_trace["retrievals"]
            if str(row["id"]) == str(lesson_read.get("retrieval_id"))
        ),
        None,
    )
    retrieval_profile_id = (
        str(retrieval["embedding_profile_id"])
        if retrieval and retrieval.get("embedding_profile_id")
        else None
    )
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT incident.id AS incident_id, incident.slug AS incident_slug,
                       job.id AS consolidation_job_id,
                       COALESCE(job.decision_id, lesson.producer_decision_id)
                           AS producer_decision_id
                FROM semantic_memories AS lesson
                LEFT JOIN consolidation_jobs AS job
                    ON job.lesson_memory_id = lesson.id
                LEFT JOIN incidents AS incident
                    ON incident.id = job.incident_id
                WHERE lesson.id = %s
                ORDER BY job.completed_at DESC NULLS LAST
                LIMIT 1
            """,
            (lesson_memory_id,),
        )
        source = cur.fetchone()
        if source is None or source["incident_id"] is None:
            cur.execute(
                """
                    SELECT incident.id AS incident_id,
                           incident.slug AS incident_slug
                    FROM incident_semantic_memories AS link
                    JOIN incidents AS incident ON incident.id = link.incident_id
                    WHERE link.memory_id = %s AND link.relationship = 'lesson'
                    LIMIT 1
                """,
                (lesson_memory_id,),
            )
            linked_incident = cur.fetchone()
        else:
            linked_incident = None
        cur.execute(
            """
                SELECT decision.id AS decision_id, decision.run_id,
                       run.incident_id, incident.slug AS incident_slug
                FROM memory_decisions AS decision
                LEFT JOIN agent_runs AS run ON run.id = decision.run_id
                LEFT JOIN incidents AS incident ON incident.id = run.incident_id
                WHERE decision.id = %s
            """,
            (decision_id,),
        )
        consumer = cur.fetchone()
        if retrieval_profile_id:
            cur.execute(
                """
                    SELECT profile_id
                    FROM semantic_memory_vectors
                    WHERE memory_id = %s AND profile_id = %s
                    LIMIT 1
                """,
                (lesson_memory_id, retrieval_profile_id),
            )
            lesson_vector = cur.fetchone()
        else:
            lesson_vector = None
        if lesson_vector is None:
            cur.execute(
                """
                    SELECT profile_id
                    FROM semantic_memory_vectors
                    WHERE memory_id = %s
                    ORDER BY embedded_at DESC
                    LIMIT 1
                """,
                (lesson_memory_id,),
            )
            lesson_vector = cur.fetchone()

    source_identity = dict(source) if source is not None else {}
    if linked_incident is not None:
        source_identity.update(dict(linked_incident))
    producer_decision_id = source_identity.get(
        "producer_decision_id", lesson_read.get("memory_producer_decision_id")
    )
    lineage_edges = [
        {
            "id": row["id"],
            "parent_read_id": row["parent_read_id"],
            "parent_memory_id": row["parent_memory_id"],
            "producer_decision_id": row["producer_decision_id"],
            "edge_type": row["edge_type"],
        }
        for row in decision_trace["lineage_edges"]
        if str(row.get("child_semantic_memory_id")) == lesson_memory_id
    ]
    profile = (
        {
            "id": retrieval["embedding_profile_id"],
            "provider": retrieval["embedding_provider"],
            "model": retrieval["embedding_model"],
            "dimensions": retrieval["embedding_dimensions"],
            "capability": retrieval["embedding_capability"],
            "encoder_revision": retrieval["encoder_revision"],
            "max_distance": retrieval["max_distance"],
        }
        if retrieval and retrieval.get("embedding_profile_id")
        else None
    )
    required_identities = (
        source_identity.get("incident_id"),
        source_identity.get("consolidation_job_id"),
        producer_decision_id,
        lesson_read.get("belief_id"),
        lesson_read.get("version_number"),
        lesson_vector["profile_id"] if lesson_vector is not None else None,
        lesson_read.get("retrieval_id"),
        lesson_read.get("id"),
        profile["id"] if profile is not None else None,
        decision_id,
    )
    if any(value is None for value in required_identities) or not lineage_edges:
        return None
    return {
        "source_incident": {
            "id": source_identity.get("incident_id"),
            "slug": source_identity.get("incident_slug"),
        },
        "consolidation": {
            "job_id": source_identity.get("consolidation_job_id"),
            "producer_decision_id": producer_decision_id,
        },
        "lesson": {
            "memory_id": lesson_read["memory_id"],
            "belief_id": lesson_read["belief_id"],
            "version_number": lesson_read["version_number"],
            "embedding_profile_id": lesson_vector["profile_id"]
            if lesson_vector is not None
            else None,
        },
        "retrieval": {
            "retrieval_id": lesson_read.get("retrieval_id"),
            "read_id": lesson_read["id"],
            "embedding_profile_id": retrieval_profile_id,
        },
        "embedding_profile": profile,
        "lineage_edges": lineage_edges,
        "consumer_decision": {
            "decision_id": decision_id,
            "run_id": consumer["run_id"] if consumer is not None else None,
            "incident_id": consumer["incident_id"] if consumer is not None else None,
            "incident_slug": consumer["incident_slug"] if consumer is not None else None,
        },
    }
