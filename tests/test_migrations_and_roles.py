"""Upgrade and product-role acceptance tests."""

from __future__ import annotations

import os
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def _database_url(name: str) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{name}"))


def _apply(conn: psycopg.Connection, paths: list[Path]) -> None:
    for path in paths:
        with conn.transaction():
            conn.execute(path.read_text())


@requires_db
def test_populated_upgrade_repairs_run_decisions_and_agent_role_can_write(monkeypatch):
    from hindsight import runs
    from hindsight.embedding_index import activate_profile, begin_profile_build, run_backfill_batch
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    database_name = f"hindsight_upgrade_{uuid4().hex}"
    target_url = _database_url(database_name)
    admin_url = _database_url("defaultdb")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        all_migrations = sorted(MIGRATIONS.glob("[0-9]*.sql"))
        legacy_paths = [path for path in all_migrations if path.name <= "0007_agent_runs.sql"]
        with psycopg.connect(target_url, autocommit=True) as conn:
            _apply(conn, legacy_paths)
            memory_id = str(uuid4())
            open_decision = f"upgrade-open:{uuid4()}"
            failed_decision = f"upgrade-failed:{uuid4()}"
            failed_lookalike_decision = f"upgrade-failed-lookalike:{uuid4()}"
            terminal_decision = f"upgrade-terminal:{uuid4()}"
            legacy_decision = f"upgrade-legacy:{uuid4()}"
            run_ids = {
                open_decision: uuid4(),
                failed_decision: uuid4(),
                failed_lookalike_decision: uuid4(),
                terminal_decision: uuid4(),
            }
            conn.execute(
                """
                    INSERT INTO semantic_memories (
                        id, namespace, content, writer, source_ref, justification
                    ) VALUES (%s, 'upgrade-test', 'legacy belief', 'legacy.agent',
                              'legacy:evidence', 'upgrade fixture')
                """,
                (memory_id,),
            )
            for decision_id, status in (
                (open_decision, "planning"),
                (failed_decision, "failed"),
                (failed_lookalike_decision, "failed"),
                (terminal_decision, "reflecting"),
            ):
                conn.execute(
                    """
                        INSERT INTO agent_runs (
                            id, thread_id, incident_slug, namespace, user_input,
                            status, decision_id
                        ) VALUES (%s, %s, %s, 'upgrade-test', 'legacy run', %s, %s)
                    """,
                    (
                        run_ids[decision_id],
                        f"thread:{decision_id}",
                        f"incident:{decision_id}",
                        status,
                        decision_id,
                    ),
                )
                if decision_id not in {failed_decision, failed_lookalike_decision}:
                    conn.execute(
                        """
                            INSERT INTO memory_reads (
                                decision_id, memory_kind, memory_id, reader, purpose
                            ) VALUES (%s, 'semantic', %s, 'legacy.agent', 'legacy read')
                        """,
                        (decision_id, memory_id),
                    )
            conn.execute(
                """
                    INSERT INTO memory_reads (
                        decision_id, memory_kind, memory_id, reader, purpose
                    ) VALUES (%s, 'semantic', %s, 'legacy.agent', 'standalone legacy read')
                """,
                (legacy_decision, memory_id),
            )

            governed_schema = [
                MIGRATIONS / "0007a_governed_memory_columns.sql",
                MIGRATIONS / "0007b_governed_memory_tables.sql",
            ]
            _apply(conn, governed_schema)
            conn.execute(
                """
                    INSERT INTO memory_decisions (
                        id, actor, decision_kind, purpose, run_id, namespace,
                        status, sealed_at
                    ) VALUES
                        (
                            %s, 'agent.run', 'agent_plan', 'Legitimate terminal decision',
                            %s, 'upgrade-test', 'sealed', now()
                        ),
                        (
                            %s, 'agent.run', 'agent_plan',
                            'Legitimate failed-run decision', %s, 'upgrade-test',
                            'sealed', now()
                        )
                """,
                (
                    terminal_decision,
                    run_ids[terminal_decision],
                    failed_lookalike_decision,
                    run_ids[failed_lookalike_decision],
                ),
            )
            conn.execute(
                """
                    INSERT INTO memory_decisions (
                        id, actor, decision_kind, purpose, status, sealed_at
                    ) VALUES (
                        %s, 'legacy.import', 'legacy_read',
                        'Backfill pre-governance memory read identity',
                        'sealed', now()
                    )
                """,
                (open_decision,),
            )

            through_0010 = [
                path
                for path in all_migrations
                if "0008_governed_memory.sql" <= path.name <= "0010_consolidation_and_benchmark.sql"
            ]
            _apply(conn, through_0010)
            canonical = conn.execute(
                """
                    SELECT actor, decision_kind, status, sealed_at
                    FROM memory_decisions WHERE id = %s
                """,
                (open_decision,),
            ).fetchone()
            assert canonical == ("agent.run", "agent_plan", "open", None)
            assert conn.execute(
                """
                    SELECT actor, decision_kind, purpose, status
                    FROM memory_decisions WHERE id = %s
                """,
                (terminal_decision,),
            ).fetchone() == (
                "agent.run",
                "agent_plan",
                "Legitimate terminal decision",
                "sealed",
            )
            assert conn.execute(
                """
                    SELECT actor, decision_kind, purpose, status
                    FROM memory_decisions WHERE id = %s
                """,
                (failed_lookalike_decision,),
            ).fetchone() == (
                "agent.run",
                "agent_plan",
                "Legitimate failed-run decision",
                "sealed",
            )
            conn.execute(
                """
                    UPDATE memory_decisions
                    SET actor = 'legacy.import', decision_kind = 'legacy_read',
                        purpose = 'Backfill pre-governance memory read identity',
                        status = 'sealed', sealed_at = now()
                    WHERE id = %s
                """,
                (open_decision,),
            )
            # Reproduce the canonical-but-sealed failed decision emitted by the
            # earlier 0008 draft for a failed run with no memory reads.
            conn.execute(
                """
                    UPDATE memory_decisions
                    SET status = 'sealed', sealed_at = now(), metadata = '{}'::JSONB
                    WHERE id = %s
                """,
                (failed_decision,),
            )
            assert conn.execute(
                """
                    SELECT actor, decision_kind, purpose, run_id, namespace, status
                    FROM memory_decisions WHERE id = %s
                """,
                (failed_decision,),
            ).fetchone() == (
                "agent.run",
                "agent_plan",
                "Backfill durable agent run decision",
                run_ids[failed_decision],
                "upgrade-test",
                "sealed",
            )

            orphan_memory_id = uuid4()
            orphan_belief_id = uuid4()
            orphan_payload = {
                "schema_version": 1,
                "thread_id": f"thread:{terminal_decision}",
                "run_id": str(run_ids[terminal_decision]),
                "incident_id": f"incident:{terminal_decision}",
                "namespace": "upgrade-test",
                "service_slug": None,
                "plan": "inspect the migrated state",
                "proposed_action": "hold changes",
                "action_approved": False,
                "retrieval_id": None,
                "recalled_memory_ids": [memory_id],
            }
            conn.execute(
                "INSERT INTO semantic_beliefs (id, namespace) VALUES (%s, 'upgrade-test')",
                (orphan_belief_id,),
            )
            conn.execute(
                """
                    INSERT INTO semantic_memories (
                        id, belief_id, version_number, namespace, content,
                        writer, source_ref, justification, producer_decision_id,
                        transition_kind, content_schema, structured_payload,
                        payload_digest, lineage_status, trust_status
                    ) VALUES (
                        %s, %s, 1, 'upgrade-test', 'orphan typed reflection',
                        'agent.reflect', %s, 'legacy reflection write', %s,
                        'assertion', 'agent_reflection.v1', %s,
                        'upgrade-reflection-digest', 'complete', 'active'
                    )
                """,
                (
                    orphan_memory_id,
                    orphan_belief_id,
                    terminal_decision,
                    terminal_decision,
                    Jsonb(orphan_payload),
                ),
            )
            conn.execute(
                "UPDATE agent_runs SET reflected_memory_id = %s WHERE id = %s",
                (orphan_memory_id, run_ids[terminal_decision]),
            )
            _apply(conn, [MIGRATIONS / "0011_repair_governed_decisions.sql"])
            repaired = conn.execute(
                """
                    SELECT actor, decision_kind, status, sealed_at
                    FROM memory_decisions WHERE id = %s
                """,
                (open_decision,),
            ).fetchone()
            assert repaired == ("agent.run", "agent_plan", "open", None)
            assert conn.execute(
                "SELECT status FROM memory_decisions WHERE id = %s", (failed_decision,)
            ).fetchone() == ("failed",)
            assert conn.execute(
                """
                    SELECT decision.opened_at = run.created_at,
                           decision.sealed_at = COALESCE(run.completed_at, run.updated_at),
                           decision.metadata->>'migrated_from'
                    FROM memory_decisions AS decision
                    JOIN agent_runs AS run ON run.id = decision.run_id
                    WHERE decision.id = %s
                """,
                (failed_decision,),
            ).fetchone() == (True, True, "agent_runs")
            assert conn.execute(
                """
                    SELECT purpose, status
                    FROM memory_decisions WHERE id = %s
                """,
                (failed_lookalike_decision,),
            ).fetchone() == ("Legitimate failed-run decision", "sealed")
            assert conn.execute(
                "SELECT status FROM memory_decisions WHERE id = %s", (legacy_decision,)
            ).fetchone() == ("sealed",)
            assert conn.execute(
                "SELECT status FROM memory_decisions WHERE id = %s", (terminal_decision,)
            ).fetchone() == ("sealed",)
            assert conn.execute(
                """
                    SELECT run_id, thread_id, incident_id, namespace, plan,
                           proposed_action, action_approved, semantic_memory_id, belief_id
                    FROM agent_reflections WHERE decision_id = %s
                """,
                (terminal_decision,),
            ).fetchone() == (
                run_ids[terminal_decision],
                f"thread:{terminal_decision}",
                f"incident:{terminal_decision}",
                "upgrade-test",
                "inspect the migrated state",
                "hold changes",
                False,
                orphan_memory_id,
                orphan_belief_id,
            )
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="terminal memory decision status is immutable",
            ):
                conn.execute(
                    """
                        UPDATE memory_decisions
                        SET status = 'open', sealed_at = NULL
                        WHERE id = %s
                    """,
                    (terminal_decision,),
                )

        provider = DeterministicEmbeddingProvider()
        building = begin_profile_build(provider=provider, db_url=target_url)
        while run_backfill_batch(
            provider=provider,
            worker_id="pytest-upgrade",
            limit=100,
            db_url=target_url,
        )["leased"]:
            pass
        activate_profile(profile_id=str(building["id"]), db_url=target_url)
        with psycopg.connect(target_url, autocommit=True) as conn:
            conn.execute((ROOT / "infra/db/roles.sql").read_text())

        with psycopg.connect(target_url) as conn:
            conn.execute("SET ROLE hindsight_agent_writer")
            conn.commit()
            monkeypatch.setattr(
                runs,
                "connect",
                lambda *_args, **_kwargs: nullcontext(conn),
            )
            role_run, created = runs.create_run(
                incident_slug=f"role-run-{uuid4()}",
                namespace="role-test",
                user_input="verify restricted run persistence",
                idempotency_key=f"role-request-{uuid4()}",
            )
            transitioned = runs.transition_run(
                run_id=role_run["id"],
                status="triaging",
                phase="triage",
                summary="Restricted role entered triage",
            )
            assert created is True
            assert transitioned["status"] == "triaging"
            assert conn.execute(
                "SELECT status FROM agent_run_events WHERE run_id = %s ORDER BY sequence",
                (role_run["id"],),
            ).fetchall() == [("queued",), ("triaging",)]
            conn.commit()

            store = MemoryStore(conn=conn, embedding_provider=provider)
            semantic = store.remember(
                memory_kind="semantic",
                namespace="role-test",
                content="agent role semantic memory",
                provenance=Provenance("pytest.agent", "evidence:role", "role acceptance"),
            )
            store.remember(
                memory_kind="episodic",
                episode_id="role-test",
                role="assistant",
                content="agent role episodic memory",
                provenance=Provenance("pytest.agent", "evidence:role", "role acceptance"),
            )
            reflection = store.remember_agent_reflection(
                decision_id=open_decision,
                run_id=str(
                    conn.execute(
                        "SELECT id FROM agent_runs WHERE decision_id = %s", (open_decision,)
                    ).fetchone()[0]
                ),
                thread_id="upgrade-reflection",
                incident_id="upgrade-incident",
                namespace="upgrade-test",
                service_slug=None,
                plan="inspect the upgraded state",
                proposed_action="hold changes",
                action_approved=False,
                content="upgraded typed reflection",
                metadata={},
                structured_payload={"schema_version": 1},
                provenance=Provenance("pytest.agent", open_decision, "verify upgraded reflection"),
                parent_memory_ids=[memory_id],
            )
            conn.commit()
            assert semantic["lineage_status"] == "complete"
            assert reflection["lineage_status"] == "complete"
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE semantic_memories SET trust_status = 'review_required' WHERE id = %s",
                    (semantic["id"],),
                )
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM semantic_memories WHERE id = %s", (semantic["id"],))
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    """
                        INSERT INTO embedding_profiles (
                            id, provider, model, dimensions, capability, encoder_revision
                        ) VALUES ('forbidden', 'test', 'test', 1024, 'semantic', 'test')
                    """
                )
            conn.rollback()
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(database_name))
            )
