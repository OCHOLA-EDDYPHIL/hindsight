"""Upgrade and product-role acceptance tests."""

from __future__ import annotations

import os
from contextlib import nullcontext
from datetime import timedelta
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


def test_agent_writer_can_fence_and_enqueue_but_cannot_administer_embedding_index():
    roles = (ROOT / "infra/db/roles.sql").read_text()
    migration = (MIGRATIONS / "0012_embedding_index_write_fence.sql").read_text()
    agent_section = roles.split("TO hindsight_agent_writer;", 3)
    select_grant, insert_grant, update_grant = agent_section[:3]

    assert "embedding_index_write_fence" in select_grant
    assert "embedding_backfill_tasks" in select_grant
    assert "embedding_backfill_tasks" in insert_grant
    assert "embedding_index_write_fence" in update_grant
    assert "embedding_backfill_tasks" not in update_grant
    assert "embedding_profiles" not in update_grant
    assert "embedding_index_state" not in update_grant

    worker_update_grant = roles.split("GRANT UPDATE ON TABLE", 2)[2].split(
        "TO hindsight_memory_worker;", 1
    )[0]
    assert "incident_semantic_beliefs" in worker_update_grant

    assert "CREATE ROLE IF NOT EXISTS hindsight_agent_writer LOGIN" in migration
    assert "CREATE ROLE IF NOT EXISTS hindsight_memory_worker LOGIN" in migration
    assert "GRANT SELECT, UPDATE ON TABLE embedding_index_write_fence" in migration
    assert "GRANT SELECT, INSERT ON TABLE embedding_backfill_tasks" in migration
    assert "GRANT UPDATE ON TABLE incident_semantic_beliefs" in migration


def test_lineage_child_producer_constraints_are_staged_for_cockroachdb():
    keys = (MIGRATIONS / "0015a_lineage_child_producer_keys.sql").read_text()
    foreign_keys = (MIGRATIONS / "0015b_lineage_child_producer_fks.sql").read_text()

    assert "semantic_memories (id, producer_decision_id)" in keys
    assert "episodic_memories (id, producer_decision_id)" in keys
    assert "memory_lineage_semantic_child_producer_fk" in foreign_keys
    assert "memory_lineage_episodic_child_producer_fk" in foreign_keys
    assert foreign_keys.count("REFERENCES") == 2


def test_run_dispatch_outbox_grants_only_required_product_role_access():
    roles = (ROOT / "infra/db/roles.sql").read_text()
    migration = (MIGRATIONS / "0017_agent_run_dispatch_outbox.sql").read_text()
    agent_sections = roles.split("TO hindsight_agent_writer;", 3)
    agent_select, agent_insert, agent_update = agent_sections[:3]
    worker_section = roles.split("TO hindsight_agent_writer;", 3)[-1]
    worker_select, worker_insert, worker_update = worker_section.split(
        "TO hindsight_memory_worker;", 3
    )[:3]

    assert "agent_run_dispatches" in agent_select
    assert "agent_run_dispatches" in agent_insert
    assert "agent_run_dispatches" in agent_update
    assert "agent_run_dispatches" in worker_select
    assert "agent_run_dispatches" not in worker_insert
    assert "agent_run_dispatches" in worker_update
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE agent_run_dispatches" in migration
    assert "GRANT SELECT, UPDATE ON TABLE agent_run_dispatches" in migration


def test_agent_run_call_budget_migration_caps_calls_and_allows_resume_replanning():
    migration = "\n".join(
        (
            (MIGRATIONS / "0026_agent_run_call_budgets.sql").read_text(),
            (MIGRATIONS / "0026a_agent_run_call_budget_guards.sql").read_text(),
        )
    )

    assert "model_call_count INT8 NOT NULL DEFAULT 0" in migration
    assert "cloudwatch_call_count INT8 NOT NULL DEFAULT 0" in migration
    assert "model_call_count BETWEEN 0 AND 4" in migration
    assert "cloudwatch_call_count BETWEEN 0 AND 3" in migration
    assert "worker_attempt_command IN ('start', 'resume')" in migration
    assert "agent_run_call_budgets_monotonic" in migration


def test_loss_safe_run_delivery_is_staged_and_tenant_fenced():
    columns_path = MIGRATIONS / "0027_loss_safe_run_delivery_columns.sql"
    guards_path = MIGRATIONS / "0027a_loss_safe_run_delivery_guards.sql"
    columns = columns_path.read_text()
    guards = guards_path.read_text()
    roles = (ROOT / "infra/db/roles.sql").read_text()

    assert columns_path.name < guards_path.name
    assert "request_fingerprint STRING" in columns
    assert "acknowledged_attempt_id UUID" in columns
    assert "CREATE TABLE IF NOT EXISTS agent_run_dispatch_attempts" in columns
    for field in (
        "id UUID PRIMARY KEY",
        "tenant_id UUID NOT NULL",
        "dispatch_id UUID NOT NULL",
        "sequence INT8 NOT NULL",
        "transport_message_id STRING",
        "worker_message_id STRING",
        "acknowledged_at TIMESTAMPTZ",
    ):
        assert field in columns

    assert "DROP INDEX IF EXISTS agent_runs@agent_runs_idempotency_key_key CASCADE" in guards
    assert "ON agent_runs (tenant_id, idempotency_key)" in guards
    assert "WHERE idempotency_key IS NOT NULL" in guards
    assert "request_fingerprint ~ '^[0-9a-f]{64}$'" in guards
    assert "agent_run_dispatch_attempts_dispatch_sequence_key" in guards
    assert "ON agent_run_dispatch_attempts (dispatch_id, sequence)" in guards
    assert "sequence >= 1" in guards
    assert "FOREIGN KEY (tenant_id, dispatch_id)" in guards
    assert "REFERENCES agent_run_dispatches (tenant_id, id)" in guards
    assert "FOREIGN KEY (tenant_id, id, acknowledged_attempt_id)" in guards
    assert "REFERENCES agent_run_dispatch_attempts (tenant_id, dispatch_id, id)" in guards
    assert "status IN ('pending', 'leased', 'sent', 'acknowledged')" in guards
    assert "status = 'acknowledged'" in guards
    assert "agent_run_dispatch_attempts_tenant_permissive" in guards
    assert "agent_run_dispatch_attempts_tenant_fence" in guards
    assert "ALTER TABLE agent_run_dispatch_attempts FORCE ROW LEVEL SECURITY" in guards
    assert "guard_agent_run_dispatch_attempt_identity" in guards
    assert "(NEW).tenant_id IS DISTINCT FROM (OLD).tenant_id" in guards
    assert "GRANT SELECT, INSERT, UPDATE ON TABLE agent_run_dispatch_attempts" in guards
    assert "GRANT SELECT, UPDATE ON TABLE agent_run_dispatch_attempts" in guards

    normalization = guards.split("UPDATE agent_run_dispatches AS dispatch", 1)[1].split(
        ";", 1
    )[0]
    assert "status = 'pending'" in normalization
    assert "attempt_count = 0" in normalization
    assert "lease_owner = NULL" in normalization
    assert "transport_message_id = NULL" in normalization
    assert "dispatched_at = NULL" in normalization

    agent_select, agent_insert, agent_update = roles.split(
        "TO hindsight_agent_writer;", 3
    )[:3]
    worker_section = roles.split("TO hindsight_agent_writer;", 3)[-1]
    worker_select, worker_insert, worker_update = worker_section.split(
        "TO hindsight_memory_worker;", 3
    )[:3]
    assert "agent_run_dispatch_attempts" in agent_select
    assert "agent_run_dispatch_attempts" in agent_insert
    assert "agent_run_dispatch_attempts" in agent_update
    assert "agent_run_dispatch_attempts" in worker_select
    assert "agent_run_dispatch_attempts" not in worker_insert
    assert "agent_run_dispatch_attempts" in worker_update


def test_product_writers_have_only_foreign_key_read_access_to_learning_preparations():
    roles = (ROOT / "infra/db/roles.sql").read_text()
    agent_select, agent_insert, agent_update = roles.split(
        "TO hindsight_agent_writer;", 3
    )[:3]
    worker_section = roles.split("TO hindsight_agent_writer;", 3)[-1]
    worker_select, worker_insert, worker_update = worker_section.split(
        "TO hindsight_memory_worker;", 3
    )[:3]

    assert "benchmark_variant_preparations" in agent_select
    assert "benchmark_variant_preparations" not in agent_insert
    assert "benchmark_variant_preparations" not in agent_update
    assert "benchmark_variant_preparations" in worker_select
    assert "benchmark_variant_preparations" not in worker_insert
    assert "benchmark_variant_preparations" not in worker_update


def test_product_principal_roles_and_prompt_safety_are_staged_fail_closed():
    columns_path = MIGRATIONS / "0028_product_identity_and_prompt_safety_columns.sql"
    guards_path = MIGRATIONS / "0028a_product_identity_and_prompt_safety_guards.sql"
    columns = columns_path.read_text()
    guards = guards_path.read_text()
    roles = (ROOT / "infra/db/roles.sql").read_text()

    assert columns_path.name < guards_path.name
    assert "CREATE TABLE IF NOT EXISTS product_principal_roles" in columns
    for field in (
        "principal_hash STRING(64) NOT NULL UNIQUE",
        "provisioning_key STRING(64) NOT NULL UNIQUE",
        "tenant_id UUID NOT NULL",
        "role STRING NOT NULL",
        "status STRING NOT NULL DEFAULT 'active'",
    ):
        assert field in columns
    assert "principal_hash ~ '^[0-9a-f]{64}$'" in columns
    assert "provisioning_key ~ '^[0-9a-f]{64}$'" in columns
    assert "role IN ('viewer', 'operator')" in columns
    assert "status IN ('active', 'revoked')" in columns
    assert "FOREIGN KEY (tenant_id) REFERENCES tenants (id)" in columns
    assert "prompt_safety_status STRING" in columns
    assert "prompt_safety_scanner_version STRING" in columns
    assert "prompt_safety_reason_codes JSONB" in columns

    assert "prompt_safety_status = 'unassessed'" in guards
    assert "legacy.unassessed" in guards
    assert "ALTER COLUMN prompt_safety_status SET NOT NULL" in guards
    assert "prompt_safety_status IN ('clear', 'suspected', 'unassessed')" in guards
    assert "jsonb_typeof(prompt_safety_reason_codes) = 'array'" in guards
    assert "(NEW).prompt_safety_status IS DISTINCT FROM (OLD).prompt_safety_status" in guards
    assert "CREATE OR REPLACE VIEW current_semantic_memories" in guards
    assert "GRANT SELECT ON TABLE product_principal_roles TO hindsight_agent_writer" in guards
    assert "CREATE POLICY" not in guards
    assert "ENABLE ROW LEVEL SECURITY" not in columns + guards

    agent_select = roles.split("TO hindsight_agent_writer;", 1)[0]
    assert "product_principal_roles" in agent_select
    lifecycle_select = roles.rsplit("TO hindsight_lifecycle;", 1)[0].rsplit(
        "GRANT SELECT ON TABLE", 1
    )[1]
    assert "product_principal_roles" in lifecycle_select


def _database_url(name: str) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{name}"))


def _apply(conn: psycopg.Connection, paths: list[Path]) -> None:
    for path in paths:
        with conn.transaction():
            conn.execute(path.read_text())


@requires_db
@pytest.mark.migration_acceptance
def test_populated_upgrade_repairs_run_decisions_and_agent_role_can_write(monkeypatch):
    from hindsight import runs
    from hindsight.agent import setup_agent_storage
    from hindsight.embedding_index import activate_profile, begin_profile_build, run_backfill_batch
    from tests.fakes import DeterministicEmbeddingProvider
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

            # Model hosted roles provisioned from the pre-0012 role template.
            # The migration must upgrade them without a manual roles.sql replay,
            # and its grants must remain safe when the SQL is retried directly.
            conn.execute("CREATE ROLE IF NOT EXISTS hindsight_agent_writer LOGIN")
            conn.execute("CREATE ROLE IF NOT EXISTS hindsight_memory_worker LOGIN")
            _apply(conn, [MIGRATIONS / "0012_embedding_index_write_fence.sql"])
            _apply(conn, [MIGRATIONS / "0012_embedding_index_write_fence.sql"])
            _apply(
                conn,
                [
                    path
                    for path in all_migrations
                    if path.name > "0012_embedding_index_write_fence.sql"
                ],
            )
            recovered_runs = conn.execute(
                """
                    SELECT id, status, worker_attempt_id, worker_attempt_count,
                           worker_attempt_command, model_call_count,
                           cloudwatch_call_count
                    FROM agent_runs
                    WHERE id IN (%s, %s)
                    ORDER BY id
                """,
                (run_ids[open_decision], run_ids[terminal_decision]),
            ).fetchall()
            assert {row[1] for row in recovered_runs} == {"queued", "resuming"}
            assert all(row[2:] == (None, 0, None, 0, 0) for row in recovered_runs)
            assert conn.execute(
                """
                    SELECT command, status FROM agent_run_dispatches
                    WHERE run_id IN (%s, %s)
                    ORDER BY command
                """,
                (run_ids[open_decision], run_ids[terminal_decision]),
            ).fetchall() == [("resume", "pending"), ("start", "pending")]
            assert conn.execute(
                """
                    SELECT count(*) FROM agent_run_events
                    WHERE run_id IN (%s, %s) AND phase = 'recovery'
                """,
                (run_ids[open_decision], run_ids[terminal_decision]),
            ).fetchone() == (2,)
            upgrade_privileges = {
                (table_name, grantee, privilege)
                for table_name, grantee, privilege in conn.execute(
                    """
                        SELECT table_name, grantee, privilege_type
                        FROM information_schema.table_privileges
                        WHERE table_schema = 'public'
                          AND table_name IN (
                              'embedding_index_write_fence',
                              'embedding_backfill_tasks',
                              'incident_semantic_beliefs'
                          )
                          AND grantee IN (
                              'hindsight_agent_writer',
                              'hindsight_memory_worker'
                          )
                    """
                ).fetchall()
            }
            assert upgrade_privileges == {
                (
                    "embedding_index_write_fence",
                    "hindsight_agent_writer",
                    "SELECT",
                ),
                (
                    "embedding_index_write_fence",
                    "hindsight_agent_writer",
                    "UPDATE",
                ),
                (
                    "embedding_backfill_tasks",
                    "hindsight_agent_writer",
                    "SELECT",
                ),
                (
                    "embedding_backfill_tasks",
                    "hindsight_agent_writer",
                    "INSERT",
                ),
                (
                    "embedding_index_write_fence",
                    "hindsight_memory_worker",
                    "SELECT",
                ),
                (
                    "embedding_index_write_fence",
                    "hindsight_memory_worker",
                    "UPDATE",
                ),
                (
                    "incident_semantic_beliefs",
                    "hindsight_memory_worker",
                    "UPDATE",
                ),
            }
            dispatch_privileges = {
                (grantee, privilege)
                for grantee, privilege in conn.execute(
                    """
                        SELECT grantee, privilege_type
                        FROM information_schema.table_privileges
                        WHERE table_schema = 'public'
                          AND table_name = 'agent_run_dispatches'
                          AND grantee IN (
                              'hindsight_agent_writer',
                              'hindsight_memory_worker'
                          )
                    """
                ).fetchall()
            }
            assert dispatch_privileges == {
                ("hindsight_agent_writer", "SELECT"),
                ("hindsight_agent_writer", "INSERT"),
                ("hindsight_agent_writer", "UPDATE"),
                ("hindsight_memory_worker", "SELECT"),
                ("hindsight_memory_worker", "UPDATE"),
            }
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

        class RestrictedRotationProvider(DeterministicEmbeddingProvider):
            provider_name = "restricted-role-rotation"
            model_name = "restricted-role-rotation-v1"
            capability = "semantic"
            encoder_revision = "restricted-role-rotation-v1"

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
        setup_agent_storage(db_url=target_url)
        with psycopg.connect(target_url, autocommit=True) as conn:
            conn.execute((ROOT / "infra/db/roles.sql").read_text())
        rotation = begin_profile_build(
            provider=RestrictedRotationProvider(),
            db_url=target_url,
        )

        with psycopg.connect(target_url) as conn:
            conn.execute(
                "SELECT set_config('hindsight.tenant_id', %s, false)",
                ("00000000-0000-0000-0000-000000000001",),
            )
            conn.execute("SET ROLE hindsight_agent_writer")
            conn.commit()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE forbidden_runtime_ddl (id INT PRIMARY KEY)")
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute("DELETE FROM semantic_memories WHERE false")
            conn.rollback()
            monkeypatch.setattr(
                runs,
                "connect",
                lambda *_args, **_kwargs: nullcontext(conn),
            )
            role_incident_slug = f"role-incident-{uuid4()}"
            runs.create_incident(
                slug=role_incident_slug,
                title="Restricted role incident",
                severity="sev2",
                summary="Verify product incident resolution",
            )
            role_run, created = runs.create_run(
                incident_slug=role_incident_slug,
                namespace="role-test",
                user_input="verify restricted run persistence",
                idempotency_key=f"role-request-{uuid4()}",
            )
            claim = runs.claim_run_attempt(
                run_id=role_run["id"],
                command="start",
                command_generation=0,
                lease_ttl=timedelta(minutes=5),
                max_attempts=3,
            )
            assert created is True
            assert claim.run["status"] == "triaging"
            assert conn.execute(
                "SELECT status FROM agent_run_events WHERE run_id = %s ORDER BY sequence",
                (role_run["id"],),
            ).fetchall() == [("queued",), ("triaging",)]
            assert conn.execute(
                """
                    SELECT command, status FROM agent_run_dispatches
                    WHERE run_id = %s
                """,
                (role_run["id"],),
            ).fetchall() == [("start", "pending")]
            conn.commit()

            resolution = runs.resolve_incident(
                slug=role_incident_slug,
                root_cause="A bounded product failure",
                action="Restore the product boundary",
                observation="The restricted API path recovered",
                recovered=True,
                actor="pytest.agent",
            )
            assert resolution["incident"]["status"] == "resolved"
            assert conn.execute(
                "SELECT count(*) FROM benchmark_variant_preparations"
            ).fetchone() == (0,)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE benchmark_variant_preparations "
                    "SET phase = 'complete' WHERE false"
                )
            conn.rollback()

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
                conn.execute(
                    """
                        UPDATE embedding_backfill_tasks
                        SET status = 'leased'
                        WHERE memory_id = %s AND profile_id = %s
                    """,
                    (semantic["id"], rotation["id"]),
                )
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE embedding_profiles SET status = 'failed' WHERE id = %s",
                    (rotation["id"],),
                )
            conn.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE embedding_index_state SET building_profile_id = NULL "
                    "WHERE singleton = true"
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
            conn.execute("RESET ROLE")
            task = conn.execute(
                """
                    SELECT status FROM embedding_backfill_tasks
                    WHERE memory_id = %s AND profile_id = %s
                """,
                (semantic["id"], rotation["id"]),
            ).fetchone()
            assert task == ("pending",)
            conn.execute("SET ROLE hindsight_memory_worker")
            assert conn.execute(
                "SELECT count(*) FROM benchmark_variant_preparations"
            ).fetchone() == (0,)
            conn.execute(
                "UPDATE semantic_memories SET t_invalid = t_invalid WHERE id = %s",
                (semantic["id"],),
            )
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE benchmark_variant_preparations "
                    "SET phase = 'complete' WHERE false"
                )
            conn.rollback()
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(database_name))
            )


@requires_db
@pytest.mark.migration_acceptance
def test_populated_prompt_safety_upgrade_is_fail_closed_and_principal_lookup_is_read_only():
    database_name = f"hindsight_safety_upgrade_{uuid4().hex}"
    target_url = _database_url(database_name)
    admin_url = _database_url("defaultdb")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        all_migrations = sorted(MIGRATIONS.glob("[0-9]*.sql"))
        pre_upgrade = [
            path
            for path in all_migrations
            if path.name <= "0027a_loss_safe_run_delivery_guards.sql"
        ]
        namespace = f"prompt-safety-upgrade-{uuid4()}"
        memory_id = uuid4()
        belief_id = uuid4()
        decision_id = f"prompt-safety-upgrade:{uuid4()}"
        with psycopg.connect(target_url, autocommit=True) as conn:
            _apply(conn, pre_upgrade)
            conn.execute(
                "INSERT INTO memory_namespaces (namespace) VALUES (%s)",
                (namespace,),
            )
            conn.execute(
                """
                    INSERT INTO memory_decisions (
                        id, actor, decision_kind, purpose, namespace, status
                    ) VALUES (%s, 'pytest', 'semantic_write',
                              'seed pre-safety memory', %s, 'open')
                """,
                (decision_id, namespace),
            )
            conn.execute(
                "INSERT INTO semantic_beliefs (id, namespace) VALUES (%s, %s)",
                (belief_id, namespace),
            )
            conn.execute(
                """
                    INSERT INTO semantic_memories (
                        id, belief_id, version_number, namespace, content, metadata,
                        writer, source_ref, justification, producer_decision_id,
                        transition_kind, content_schema, structured_payload,
                        payload_digest, lineage_status, trust_status
                    ) VALUES (
                        %s, %s, 1, %s, 'legacy approved guidance',
                        '{"operator_disposition":"approved","safety_status":"safe",
                          "contradiction_status":"supported",
                          "usage_instruction":"positive_guidance"}'::JSONB,
                        'pytest', 'evidence:legacy', 'pre-safety fixture', %s,
                        'assertion', 'semantic.v1', '{}'::JSONB,
                        'legacy-digest', 'complete', 'active'
                    )
                """,
                (memory_id, belief_id, namespace, decision_id),
            )
            conn.execute(
                "UPDATE memory_decisions SET status = 'sealed', sealed_at = now() WHERE id = %s",
                (decision_id,),
            )

            _apply(
                conn,
                [
                    MIGRATIONS / "0028_product_identity_and_prompt_safety_columns.sql",
                    MIGRATIONS / "0028a_product_identity_and_prompt_safety_guards.sql",
                ],
            )

            upgraded = conn.execute(
                """
                    SELECT prompt_safety_status, prompt_safety_scanner_version,
                           prompt_safety_reason_codes
                    FROM semantic_memories WHERE id = %s
                """,
                (memory_id,),
            ).fetchone()
            assert upgraded == (
                "unassessed",
                "legacy.unassessed",
                ["legacy_unassessed"],
            )
            assert conn.execute(
                "SELECT prompt_safety_status FROM current_semantic_memories WHERE id = %s",
                (memory_id,),
            ).fetchone() == ("unassessed",)

            with pytest.raises(
                psycopg.errors.RaiseException, match="prompt safety are immutable"
            ):
                conn.execute(
                    "UPDATE semantic_memories "
                    "SET prompt_safety_scanner_version = 'tampered.v1' WHERE id = %s",
                    (memory_id,),
                )

            principal_hash = "a" * 64
            provisioning_key = "b" * 64
            conn.execute(
                """
                    INSERT INTO product_principal_roles (
                        principal_hash, provisioning_key, tenant_id, role, status
                    ) VALUES (%s, %s, '00000000-0000-0000-0000-000000000001',
                              'operator', 'active')
                """,
                (principal_hash, provisioning_key),
            )
            conn.execute("SET ROLE hindsight_agent_writer")
            assert conn.execute(
                "SELECT role, status FROM product_principal_roles WHERE principal_hash = %s",
                (principal_hash,),
            ).fetchone() == ("operator", "active")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "UPDATE product_principal_roles SET status = 'revoked' "
                    "WHERE principal_hash = %s",
                    (principal_hash,),
                )
            conn.execute("RESET ROLE")
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(
                    sql.Identifier(database_name)
                )
            )


def _seed_tenant_vector_fixture(conn: psycopg.Connection) -> tuple[str, str, str, str]:
    from hindsight.embeddings import EMBEDDING_DIMENSIONS, vector_literal

    tenant_id = "00000000-0000-0000-0000-000000000001"
    other_tenant_id = "00000000-0000-0000-0000-000000000279"
    namespace = "vector-qualification-target"
    profile_id = "vector-qualification-profile"
    target_memory_id = str(uuid4())
    target_vector = vector_literal([1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))])
    distractor_vector = vector_literal([0.0, 1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 2))])

    conn.execute(
        """
            INSERT INTO tenants (id, slug, tenant_kind)
            VALUES (%s, 'vector-qualification-distractor', 'diagnostic')
            ON CONFLICT (id) DO NOTHING
        """,
        (other_tenant_id,),
    )
    conn.execute(
        """
            INSERT INTO embedding_profiles (
                id, provider, model, dimensions, capability, encoder_revision,
                status, activated_at
            ) VALUES
                (%s, 'pytest', 'target', 1024, 'semantic', 'v1', 'active', now()),
                ('vector-qualification-other-profile', 'pytest', 'other', 1024,
                 'semantic', 'v1', 'active', now())
            ON CONFLICT (id) DO NOTHING
        """,
        (profile_id,),
    )
    conn.execute("SET experimental_enable_temp_tables = 'on'")
    conn.execute(
        """
            CREATE TEMP TABLE vector_qualification_fixture (
                memory_id UUID PRIMARY KEY,
                tenant_id UUID NOT NULL,
                namespace STRING NOT NULL,
                profile_id STRING NOT NULL,
                embedding VECTOR(1024) NOT NULL,
                fixture_kind STRING NOT NULL
            )
        """
    )
    rows = [
        (target_memory_id, tenant_id, namespace, profile_id, target_vector, "target"),
        *(
            (
                str(uuid4()),
                tenant_id,
                namespace,
                profile_id,
                distractor_vector,
                "same-prefix-distractor",
            )
            for _ in range(999)
        ),
        (
            str(uuid4()),
            other_tenant_id,
            namespace,
            profile_id,
            target_vector,
            "tenant-distractor",
        ),
        (
            str(uuid4()),
            tenant_id,
            "vector-qualification-other-namespace",
            profile_id,
            target_vector,
            "namespace-distractor",
        ),
        (
            str(uuid4()),
            tenant_id,
            namespace,
            "vector-qualification-other-profile",
            target_vector,
            "profile-distractor",
        ),
    ]
    with conn.cursor().copy(
        "COPY vector_qualification_fixture "
        "(memory_id, tenant_id, namespace, profile_id, embedding, fixture_kind) FROM STDIN"
    ) as copy:
        for row in rows:
            copy.write_row(row)

    conn.execute(
        """
            INSERT INTO memory_namespaces (tenant_id, namespace)
            SELECT DISTINCT tenant_id, namespace
            FROM vector_qualification_fixture
            ON CONFLICT (namespace) DO NOTHING
        """
    )
    conn.execute(
        """
            INSERT INTO memory_decisions (
                id, tenant_id, actor, decision_kind, purpose, namespace,
                status
            )
            SELECT 'vector-qualification:' || memory_id::STRING, tenant_id,
                   'pytest', 'semantic_write', 'qualify vector index', namespace,
                   'open'
            FROM vector_qualification_fixture
        """
    )
    conn.execute(
        """
            INSERT INTO semantic_beliefs (id, tenant_id, namespace)
            SELECT memory_id, tenant_id, namespace
            FROM vector_qualification_fixture
        """
    )
    conn.execute(
        """
            INSERT INTO semantic_memories (
                id, tenant_id, belief_id, version_number, namespace, content,
                writer, source_ref, justification, producer_decision_id,
                transition_kind, content_schema, structured_payload,
                payload_digest, lineage_status, trust_status,
                prompt_safety_status, prompt_safety_scanner_version,
                prompt_safety_reason_codes
            )
            SELECT memory_id, tenant_id, memory_id, 1, namespace, fixture_kind,
                   'pytest', 'vector-qualification', 'qualify vector index',
                   'vector-qualification:' || memory_id::STRING, 'assertion',
                   'semantic.v1', '{}'::JSONB, memory_id::STRING, 'complete',
                   'active', 'clear', 'pytest.v1', '[]'::JSONB
            FROM vector_qualification_fixture
        """
    )
    conn.execute(
        """
            INSERT INTO semantic_memory_vectors (
                tenant_id, memory_id, profile_id, namespace, content_digest, embedding
            )
            SELECT tenant_id, memory_id, profile_id, namespace,
                   memory_id::STRING, embedding
            FROM vector_qualification_fixture
        """
    )
    conn.execute(
        """
            UPDATE memory_decisions
            SET status = 'sealed', sealed_at = now()
            WHERE id IN (
                SELECT 'vector-qualification:' || memory_id::STRING
                FROM vector_qualification_fixture
            )
        """
    )
    conn.execute("ANALYZE semantic_memory_vectors")
    return tenant_id, namespace, profile_id, target_memory_id


def _assert_tenant_vector_qualification(conn: psycopg.Connection) -> None:
    from hindsight.embeddings import EMBEDDING_DIMENSIONS, vector_literal
    from hindsight.vector_index_qualification import (
        TENANT_VECTOR_INDEX,
        explain_semantic_vector_search,
        qualify_semantic_vector_plan,
    )

    tenant_id, namespace, profile_id, target_memory_id = _seed_tenant_vector_fixture(conn)
    index_names = {row[1] for row in conn.execute("SHOW INDEXES FROM semantic_memory_vectors")}
    assert "semantic_memory_vectors_embedding_idx" in index_names
    assert TENANT_VECTOR_INDEX in index_names

    query_vector = [1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))]
    plan = explain_semantic_vector_search(
        conn,
        tenant_id=tenant_id,
        namespace=namespace,
        profile_id=profile_id,
        query_vector=query_vector,
        limit=5,
    )
    assert qualify_semantic_vector_plan(plan)
    rows = conn.execute(
        f"""
            SELECT memory_id
            FROM semantic_memory_vectors
            WHERE tenant_id = %s::UUID
                AND namespace = %s
                AND profile_id = %s
            ORDER BY embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS})
            LIMIT 5
        """,
        (tenant_id, namespace, profile_id, vector_literal(query_vector)),
    ).fetchall()
    assert str(rows[0][0]) == target_memory_id


@requires_db
@pytest.mark.migration_acceptance
def test_tenant_vector_index_fresh_and_populated_upgrade_qualification():
    from scripts.migrate import apply_migrations
    from hindsight.vector_index_qualification import TENANT_VECTOR_INDEX

    database_names = (
        f"hindsight_vector_fresh_{uuid4().hex}",
        f"hindsight_vector_populated_{uuid4().hex}",
    )
    admin_url = _database_url("defaultdb")
    try:
        fresh_url = _database_url(database_names[0])
        apply_migrations(fresh_url)
        with psycopg.connect(fresh_url, autocommit=True) as conn:
            _assert_tenant_vector_qualification(conn)

        populated_url = _database_url(database_names[1])
        apply_migrations(populated_url, through="0029e_product_credential_locators.sql")
        with psycopg.connect(populated_url, autocommit=True) as conn:
            fixture = _seed_tenant_vector_fixture(conn)
            index_names = {
                row[1] for row in conn.execute("SHOW INDEXES FROM semantic_memory_vectors")
            }
            assert "semantic_memory_vectors_embedding_idx" in index_names
            assert TENANT_VECTOR_INDEX not in index_names
        apply_migrations(populated_url)
        with psycopg.connect(populated_url, autocommit=True) as conn:
            tenant_id, namespace, profile_id, target_memory_id = fixture
            index_names = {
                row[1] for row in conn.execute("SHOW INDEXES FROM semantic_memory_vectors")
            }
            assert "semantic_memory_vectors_embedding_idx" in index_names
            assert TENANT_VECTOR_INDEX in index_names
            from hindsight.embeddings import EMBEDDING_DIMENSIONS, vector_literal
            from hindsight.vector_index_qualification import (
                explain_semantic_vector_search,
                qualify_semantic_vector_plan,
            )

            plan = explain_semantic_vector_search(
                conn,
                tenant_id=tenant_id,
                namespace=namespace,
                profile_id=profile_id,
                query_vector=[1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))],
                limit=5,
            )
            assert qualify_semantic_vector_plan(plan)
            rows = conn.execute(
                f"""
                    SELECT memory_id
                    FROM semantic_memory_vectors
                    WHERE tenant_id = %s::UUID
                        AND namespace = %s
                        AND profile_id = %s
                    ORDER BY embedding <=> %s::VECTOR({EMBEDDING_DIMENSIONS})
                    LIMIT 5
                """,
                (
                    tenant_id,
                    namespace,
                    profile_id,
                    vector_literal([1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))]),
                ),
            ).fetchall()
            assert str(rows[0][0]) == target_memory_id
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            for database_name in database_names:
                admin.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(
                        sql.Identifier(database_name)
                    )
                )
