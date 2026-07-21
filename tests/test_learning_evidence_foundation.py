import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "0023_learning_evidence_foundation.sql"
requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def test_learning_evidence_schema_is_tenant_bound_and_append_only():
    migration = MIGRATION.read_text()

    assert "00000000-0000-0000-0000-000000000004" in migration
    assert "'learning', 'learning'" in migration
    for table in (
        "learning_protocol_authorizations",
        "learning_execution_authorizations",
        "learning_evidence_records",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration
        assert f"CREATE POLICY IF NOT EXISTS {table}_tenant_permissive" in migration
        assert f"CREATE POLICY IF NOT EXISTS {table}_tenant_fence" in migration
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in migration
    assert "learning_protocol_authorization_immutable" in migration
    assert "learning_evidence_record_immutable" in migration
    assert "learning_execution_authorization_guarded" in migration
    assert "sequence IN (1, 2)" in migration
    assert "protocol-v3-reset-1" in migration


def test_learning_authorization_bindings_preserve_legacy_experiments():
    migration = MIGRATION.read_text()
    fixture = (ROOT / "scripts" / "populated_upgrade_fixture.py").read_text()

    assert "ADD COLUMN IF NOT EXISTS protocol_authorization_id UUID" in migration
    assert "ADD COLUMN IF NOT EXISTS execution_authorization_id UUID" in migration
    assert "SET protocol_authorization_id" not in migration
    assert "SET execution_authorization_id" not in migration
    assert "protocol_authorization_id, execution_authorization_id" in fixture
    assert "protocol_authorization_id IS DISTINCT FROM" in migration
    assert "execution_authorization_id IS DISTINCT FROM" in migration


def test_product_roles_cannot_write_learning_authority_or_evidence():
    roles = (ROOT / "infra" / "db" / "roles.sql").read_text()
    restricted = roles.split("REVOKE ALL ON TABLE", 1)[1].split("TO hindsight_archive;", 1)[0]

    for table in (
        "learning_protocol_authorizations",
        "learning_execution_authorizations",
        "learning_evidence_records",
    ):
        assert table in restricted
    for role in (
        "hindsight_agent_writer",
        "hindsight_memory_worker",
        "hindsight_mcp_readonly",
        "hindsight_dashboard_reader",
        "hindsight_cdc",
    ):
        assert role in restricted
    assert "GRANT SELECT ON TABLE" in roles


def _expect_database_error(conn, statement, params=()):
    with pytest.raises(psycopg.Error):
        with conn.transaction():
            conn.execute(statement, params)


@requires_db
def test_learning_authority_transitions_and_evidence_are_immutable():
    from hindsight.db import connect, database_url
    from hindsight.server_tenants import ACCEPTANCE_TENANT_ID, learning_tenant_id
    from hindsight.tenant import tenant_scope

    protocol_id = uuid4()
    execution_id = uuid4()
    evidence_id = uuid4()
    suffix = uuid4().hex
    with tenant_scope(learning_tenant_id()):
        with connect(database_url()) as conn:
            conn.execute(
                """
                    INSERT INTO learning_protocol_authorizations (
                        id, authorization_slot, authorization_payload,
                        authorization_sha256, protocol_schema_version,
                        protocol_identity_sha256, corpus_sha256, code_sha,
                        reasoning_provider, reasoning_model,
                        embedding_profile_id, embedding_provider, embedding_model,
                        embedding_max_distance, qualification_run_id,
                        qualification_evidence_sha256, product_run_id,
                        product_provenance_sha256, authorized_by,
                        authorization_workflow_run_id,
                        authorization_workflow_run_attempt, archive_bucket,
                        archive_key, archive_version_id, archive_sha256
                    ) VALUES (
                        %s, 'protocol-v3-reset-1', %s, %s, 3, %s, %s, %s,
                        'gemini', 'gemini-3.1-flash-lite', %s, 'gemini',
                        'gemini-embedding-2', 0.35, 1, %s, 2, %s, 'owner',
                        3, 1, 'bucket', %s, %s, %s
                    )
                """,
                (
                    protocol_id,
                    Jsonb({"slot": "protocol-v3-reset-1"}),
                    f"auth-{suffix}",
                    f"protocol-{suffix}",
                    f"corpus-{suffix}",
                    "a" * 40,
                    f"profile-{suffix}",
                    f"qualification-{suffix}",
                    f"product-{suffix}",
                    f"learning/reset/{suffix}",
                    f"version-{suffix}",
                    f"archive-{suffix}",
                ),
            )
            conn.execute(
                """
                    INSERT INTO learning_execution_authorizations (
                        id, protocol_authorization_id, sequence,
                        authorization_payload, authorization_sha256,
                        authorization_workflow_run_id,
                        authorization_workflow_run_attempt,
                        authorization_archive_key,
                        authorization_archive_version_id,
                        authorization_archive_sha256
                    ) VALUES (%s, %s, 1, %s, %s, 3, 1, %s, %s, %s)
                """,
                (
                    execution_id,
                    protocol_id,
                    Jsonb({"sequence": 1}),
                    f"execution-{suffix}",
                    f"learning/execution/{suffix}",
                    f"version-execution-{suffix}",
                    f"execution-archive-{suffix}",
                ),
            )
            _expect_database_error(
                conn,
                "UPDATE learning_execution_authorizations SET status = 'finalized' WHERE id = %s",
                (execution_id,),
            )
            conn.execute(
                """
                    UPDATE learning_execution_authorizations
                    SET status = 'consumed', consumer_workflow_run_id = 4,
                        consumer_workflow_run_attempt = 1,
                        consumer_code_sha = %s, consumption_payload = %s,
                        consumption_sha256 = %s, consumption_archive_key = %s,
                        consumption_archive_version_id = %s, consumed_at = now()
                    WHERE id = %s
                """,
                (
                    "a" * 40,
                    Jsonb({"run": 4}),
                    f"consumption-{suffix}",
                    f"learning/consumption/{suffix}",
                    f"version-consumption-{suffix}",
                    execution_id,
                ),
            )
            conn.execute(
                """
                    UPDATE learning_execution_authorizations
                    SET status = 'finalized',
                        terminal_class = 'infrastructure_outcome_free',
                        terminal_reason = 'test terminalization',
                        terminal_evidence_sha256 = %s, finalized_at = now()
                    WHERE id = %s
                """,
                (f"terminal-{suffix}", execution_id),
            )
            _expect_database_error(
                conn,
                "UPDATE learning_execution_authorizations SET terminal_reason = 'changed' WHERE id = %s",
                (execution_id,),
            )
            conn.execute(
                """
                    INSERT INTO learning_evidence_records (
                        id, evidence_kind, result, protocol_valid, reason_code,
                        code_sha, protocol_identity_sha256,
                        protocol_authorization_id, execution_authorization_id,
                        workflow_name, workflow_run_id, workflow_run_attempt,
                        canonical_report, canonical_report_sha256, archive_bucket,
                        manifest_key, manifest_version_id, manifest_sha256,
                        retain_until
                    ) VALUES (
                        %s, 'study', 'inconclusive', true, 'test', %s, %s,
                        %s, %s, 'learning evidence', 4, 1, %s, %s,
                        'bucket', %s, %s, %s, now() + INTERVAL '7 years'
                    )
                """,
                (
                    evidence_id,
                    "a" * 40,
                    f"protocol-{suffix}",
                    protocol_id,
                    execution_id,
                    b'{"result":"inconclusive"}',
                    f"report-{suffix}",
                    f"learning/evidence/{suffix}",
                    f"version-evidence-{suffix}",
                    f"manifest-{suffix}",
                ),
            )
            _expect_database_error(
                conn,
                "UPDATE learning_evidence_records SET result = 'accepted' WHERE id = %s",
                (evidence_id,),
            )
            _expect_database_error(
                conn,
                "DELETE FROM learning_protocol_authorizations WHERE id = %s",
                (protocol_id,),
            )
            conn.commit()

    role = f"learning_evidence_rls_{suffix}"
    with psycopg.connect(database_url(), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE ROLE {} NOBYPASSRLS").format(sql.Identifier(role)))
        admin.execute(
            sql.SQL("GRANT SELECT ON learning_evidence_records TO {}").format(sql.Identifier(role))
        )
    try:
        with psycopg.connect(database_url()) as conn:
            conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            conn.execute(
                "SELECT set_config('hindsight.tenant_id', %s, false)",
                (ACCEPTANCE_TENANT_ID,),
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM learning_evidence_records WHERE id = %s",
                    (evidence_id,),
                ).fetchone()[0]
                == 0
            )
    finally:
        with psycopg.connect(database_url(), autocommit=True) as admin:
            admin.execute(
                sql.SQL("REVOKE SELECT ON learning_evidence_records FROM {}").format(
                    sql.Identifier(role)
                )
            )
            admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@requires_db
@pytest.mark.migration_acceptance
def test_qualification_family_authority_migration_is_executable_and_immutable():
    base_url = os.environ["DATABASE_URL"]
    parts = urlsplit(base_url)
    database_name = f"hindsight_qualification_authority_{uuid4().hex}"
    admin_url = urlunsplit(parts._replace(path="/defaultdb"))
    target_url = urlunsplit(parts._replace(path=f"/{database_name}"))
    attempt_id = uuid4()
    family_sha256 = uuid4().hex * 2

    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    try:
        with psycopg.connect(target_url, autocommit=True) as conn:
            for path in sorted((ROOT / "migrations").glob("[0-9]*.sql")):
                with conn.transaction():
                    conn.execute(path.read_text())
            conn.execute(
                "SELECT set_config('hindsight.tenant_id', %s, false)",
                ("00000000-0000-0000-0000-000000000004",),
            )
            conn.execute(
                """
                INSERT INTO learning_qualification_attempts (
                    id, family_sha256, sequence, family_contract, status,
                    authorization_payload, authorization_sha256,
                    authorization_archive_key, authorization_archive_version_id,
                    consumption_payload, consumption_sha256,
                    consumption_archive_key, consumption_archive_version_id,
                    consumer_workflow_run_id, consumer_workflow_run_attempt,
                    consumer_code_sha, consumed_at
                ) VALUES (
                    %s, %s, 1, %s, 'consumed', %s, %s, %s, %s,
                    %s, %s, %s, %s, 7, 1, %s, now()
                )
                """,
                (
                    attempt_id,
                    family_sha256,
                    Jsonb({"schema_version": 1, "corpus_sha256": "a" * 64}),
                    Jsonb({"attempt_id": str(attempt_id)}),
                    f"authorization-{family_sha256}",
                    f"learning/qualification/{family_sha256}/authorization",
                    "authorization-version",
                    Jsonb({"attempt_id": str(attempt_id)}),
                    f"consumption-{family_sha256}",
                    f"learning/qualification/{family_sha256}/consumption",
                    "consumption-version",
                    "b" * 40,
                ),
            )
            _expect_database_error(
                conn,
                "UPDATE learning_qualification_attempts SET sequence = 2 WHERE id = %s",
                (attempt_id,),
            )
            conn.execute(
                """
                UPDATE learning_qualification_attempts
                SET status = 'finalized', qualification_status = 'scientific_failed',
                    terminal_class = 'scientific_failed', finalization_payload = %s,
                    finalization_sha256 = %s, finalization_archive_key = %s,
                    finalization_archive_version_id = %s, finalized_at = now()
                WHERE id = %s
                """,
                (
                    Jsonb({"terminal_class": "scientific_failed"}),
                    f"finalization-{family_sha256}",
                    f"learning/qualification/{family_sha256}/finalization",
                    "finalization-version",
                    attempt_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO learning_qualification_family_terminals (
                    family_sha256, family_contract, terminal_class,
                    qualification_status, terminal_payload, terminal_sha256,
                    archive_bucket, archive_key, archive_version_id,
                    manifest_key, manifest_version_id, manifest_sha256
                ) VALUES (%s, %s, 'scientific_failed', 'scientific_failed',
                    %s, %s, 'bucket', %s, 'terminal-version',
                    %s, 'manifest-version', %s)
                """,
                (
                    family_sha256,
                    Jsonb({"schema_version": 1, "corpus_sha256": "a" * 64}),
                    Jsonb({"terminal_class": "scientific_failed"}),
                    f"terminal-{family_sha256}",
                    f"learning/qualification/{family_sha256}/terminal",
                    f"learning/evidence/{family_sha256}/manifest",
                    f"manifest-{family_sha256}",
                ),
            )
            _expect_database_error(
                conn,
                "DELETE FROM learning_qualification_family_terminals WHERE family_sha256 = %s",
                (family_sha256,),
            )
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(database_name))
            )
