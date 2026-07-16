"""Executable upgrade coverage for the governed benchmark protocol."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"
requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def _load_benchmark_script():
    spec = importlib.util.spec_from_file_location(
        "hindsight_protocol_benchmark_script",
        ROOT / "scripts" / "run_learning_benchmark.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _database_url(name: str) -> str:
    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{name}"))


def _create_preserved_database(prefix: str) -> str:
    """Create an isolated database without deleting it after the acceptance run."""

    database_name = f"{prefix}_{uuid4().hex}"
    with psycopg.connect(_database_url("defaultdb"), autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    return _database_url(database_name)


def _apply(conn: psycopg.Connection, paths: list[Path]) -> None:
    for path in paths:
        with conn.transaction():
            conn.execute(path.read_text())


def _insert_experiment(
    conn: psycopg.Connection,
    *,
    experiment_id: UUID,
    kind: str,
    status: str,
    preregistration: dict[str, object] | None = None,
    preregistration_sha256: str | None = None,
    study_key_sha256: str | None = None,
    claim_family_sha256: str | None = None,
    code_sha: str = "code-sha",
) -> None:
    conn.execute(
        """
            INSERT INTO benchmark_experiments (
                id, experiment_kind, status, manifest, manifest_sha256,
                preregistration, preregistration_sha256, provider, model,
                study_key_sha256, claim_family_sha256, code_sha
            ) VALUES (
                %s, %s, %s, '{}'::JSONB, 'manifest', %s, %s,
                'gemini', 'gemini-live', %s, %s, %s
            )
        """,
        (
            experiment_id,
            kind,
            status,
            Jsonb(preregistration) if preregistration is not None else None,
            preregistration_sha256,
            study_key_sha256,
            claim_family_sha256,
            code_sha,
        ),
    )


def test_protocol_schema_and_guards_are_split_across_commits():
    schema = (MIGRATIONS / "0013_benchmark_protocol_integrity.sql").read_text()
    guards = (MIGRATIONS / "0014_benchmark_protocol_guards.sql").read_text()

    assert "benchmark_variant_preparations" in schema
    assert "benchmark_confirmation_bindings" in schema
    assert "binding_sequence INT8 NOT NULL" in schema
    assert "consolidation_policy" in schema
    assert "confirmation_experiment_id UUID UNIQUE REFERENCES benchmark_experiments" in schema
    assert "'gold_lesson'" in schema
    assert "'reference_lesson'" in schema
    assert "CREATE TRIGGER" not in schema
    assert "INSERT INTO benchmark_confirmation_preregistrations" not in schema

    assert "INSERT INTO benchmark_confirmation_preregistrations" in guards
    assert "AFTER INSERT ON benchmark_experiments" in guards
    assert "direct binding requires an existing matching confirmation" in guards
    assert "benchmark_experiments_study_kind_idx" in guards
    assert "benchmark_experiments_claim_family_kind_active_idx" in guards
    assert "benchmark_confirmation_bindings_pilot_idx" in guards
    assert "pilot.id::STRING = confirmation.preregistration->>'pilot_experiment_id'" in guards
    assert "DROP TRIGGER IF EXISTS benchmark_experiment_contract_immutable" in guards
    assert "DROP TRIGGER IF EXISTS benchmark_trial_trace_immutable" in guards
    assert "benchmark_variant_preparation_delete_immutable" in guards
    assert "'scientific_failed', 'infrastructure_failed'" in schema
    assert "(OLD).status IN ('completed', 'scientific_failed', 'infrastructure_failed')" in guards


@requires_db
def test_upgrade_from_0012_backfills_the_first_confirmation_after_schema_commit():
    target_url = _create_preserved_database("hindsight_benchmark_upgrade")
    all_migrations = sorted(MIGRATIONS.glob("[0-9]*.sql"))
    through_0012 = [
        path for path in all_migrations if path.name <= "0012_embedding_index_write_fence.sql"
    ]
    pilot_id = uuid4()
    first_confirmation_id = uuid4()
    duplicate_confirmation_id = uuid4()
    malformed_confirmation_id = uuid4()
    digest_collision_pilot_id = uuid4()
    digest_collision_confirmation_id = uuid4()
    malformed_preregistration_sha256 = f"malformed-{uuid4().hex}"
    legacy_incident_slug = f"legacy-incident-{uuid4().hex}"
    preregistration_sha256 = f"prereg-{uuid4().hex}"
    preregistration = {
        "pilot_experiment_id": str(pilot_id),
        "sha256": preregistration_sha256,
    }

    with psycopg.connect(target_url, autocommit=True) as conn:
        _apply(conn, through_0012)
        conn.execute(
            """
                INSERT INTO incidents (
                    slug, title, severity, status, started_at, summary
                ) VALUES (%s, 'Legacy incident', 'sev2', 'open', now(), 'legacy')
            """,
            (legacy_incident_slug,),
        )
        conn.execute(
            """
                INSERT INTO benchmark_experiments (
                    id, experiment_kind, status, manifest, manifest_sha256,
                    provider, model, created_at
                ) VALUES (
                    %s, 'pilot', 'completed', '{}'::JSONB, 'pilot-manifest',
                    'gemini', 'gemini-live', '2026-01-01T00:00:00Z'
                )
            """,
            (pilot_id,),
        )
        conn.execute(
            """
                INSERT INTO benchmark_experiments (
                    id, experiment_kind, status, manifest, manifest_sha256,
                    provider, model, created_at
                ) VALUES (
                    %s, 'pilot', 'completed', '{}'::JSONB, 'collision-pilot-manifest',
                    'gemini', 'gemini-live', '2026-01-01T12:00:00Z'
                )
            """,
            (digest_collision_pilot_id,),
        )
        for confirmation_id, created_at in (
            (first_confirmation_id, "2026-01-02T00:00:00Z"),
            (duplicate_confirmation_id, "2026-01-03T00:00:00Z"),
        ):
            conn.execute(
                """
                    INSERT INTO benchmark_experiments (
                        id, experiment_kind, status, manifest, manifest_sha256,
                        preregistration, preregistration_sha256, provider, model,
                        created_at
                    ) VALUES (
                        %s, 'confirmation', 'completed', '{}'::JSONB,
                        'confirmation-manifest', %s, %s, 'gemini',
                        'gemini-live', %s
                    )
                """,
                (
                    confirmation_id,
                    Jsonb(preregistration),
                    preregistration_sha256,
                    created_at,
                ),
            )
        conn.execute(
            """
                INSERT INTO benchmark_experiments (
                    id, experiment_kind, status, manifest, manifest_sha256,
                    preregistration, preregistration_sha256, provider, model,
                    created_at
                ) VALUES (
                    %s, 'confirmation', 'completed', '{}'::JSONB,
                    'collision-confirmation-manifest', %s, %s, 'gemini',
                    'gemini-live', '2026-01-02T12:00:00Z'
                )
            """,
            (
                digest_collision_confirmation_id,
                Jsonb(
                    {
                        "pilot_experiment_id": str(digest_collision_pilot_id),
                        "sha256": preregistration_sha256,
                    }
                ),
                preregistration_sha256,
            ),
        )
        conn.execute(
            """
                INSERT INTO benchmark_experiments (
                    id, experiment_kind, status, manifest, manifest_sha256,
                    preregistration, preregistration_sha256, provider, model,
                    created_at
                ) VALUES (
                    %s, 'confirmation', 'completed', '{}'::JSONB,
                    'malformed-confirmation-manifest', %s, %s, 'gemini',
                    'gemini-live', '2026-01-04T00:00:00Z'
                )
            """,
            (
                malformed_confirmation_id,
                Jsonb(
                    {
                        "pilot_experiment_id": "not-a-uuid",
                        "sha256": malformed_preregistration_sha256,
                    }
                ),
                malformed_preregistration_sha256,
            ),
        )
        conn.execute(
            """
                INSERT INTO benchmark_trials (
                    experiment_id, variant_id, repetition, arm, namespace, status
                ) VALUES (%s, 'legacy-variant', 1, 'gold_lesson', 'legacy', 'completed')
            """,
            (pilot_id,),
        )

        _apply(conn, [MIGRATIONS / "0013_benchmark_protocol_integrity.sql"])
        assert conn.execute(
            "SELECT count(*) FROM benchmark_confirmation_preregistrations"
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT study_key_sha256, code_sha FROM benchmark_experiments WHERE id = %s",
            (pilot_id,),
        ).fetchone() == (None, None)
        assert conn.execute(
            "SELECT consolidation_policy FROM incidents WHERE slug = %s",
            (legacy_incident_slug,),
        ).fetchone() == ("managed",)

        _apply(conn, [MIGRATIONS / "0014_benchmark_protocol_guards.sql"])
        bound = conn.execute(
            """
                SELECT confirmation_experiment_id
                FROM benchmark_confirmation_preregistrations
                WHERE pilot_experiment_id = %s
            """,
            (pilot_id,),
        ).fetchone()
        assert bound == (first_confirmation_id,)
        assert {
            (row[0], row[1])
            for row in conn.execute(
                """
                    SELECT confirmation_experiment_id, binding_sequence
                    FROM benchmark_confirmation_bindings
                    WHERE pilot_experiment_id = %s
                """,
                (pilot_id,),
            ).fetchall()
        } == {(first_confirmation_id, 1), (duplicate_confirmation_id, 2)}
        assert conn.execute(
            """
                SELECT count(*) FROM benchmark_confirmation_preregistrations
                WHERE preregistration_sha256 = %s
            """,
            (malformed_preregistration_sha256,),
        ).fetchone() == (0,)
        assert conn.execute(
            """
                SELECT count(*) FROM benchmark_confirmation_preregistrations
                WHERE pilot_experiment_id = %s
            """,
            (digest_collision_pilot_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            "SELECT count(*) FROM benchmark_experiments WHERE experiment_kind = 'confirmation'"
        ).fetchone() == (4,)
        assert conn.execute(
            "SELECT arm FROM benchmark_trials WHERE experiment_id = %s",
            (pilot_id,),
        ).fetchone() == ("gold_lesson",)


@requires_db
def test_fresh_protocol_enforces_binding_study_and_preparation_invariants():
    target_url = _create_preserved_database("hindsight_benchmark_fresh")
    with psycopg.connect(target_url, autocommit=True) as conn:
        _apply(conn, sorted(MIGRATIONS.glob("[0-9]*.sql")))

        managed_incident_slug = f"managed-{uuid4().hex}"
        manual_incident_slug = f"manual-{uuid4().hex}"
        assert conn.execute(
            """
                INSERT INTO incidents (
                    slug, title, severity, status, started_at, summary
                ) VALUES (%s, 'Managed', 'sev2', 'open', now(), 'managed')
                RETURNING consolidation_policy
            """,
            (managed_incident_slug,),
        ).fetchone() == ("managed",)
        assert conn.execute(
            """
                INSERT INTO incidents (
                    slug, title, severity, status, started_at, summary,
                    consolidation_policy
                ) VALUES (%s, 'Manual', 'sev2', 'open', now(), 'manual', 'manual')
                RETURNING consolidation_policy
            """,
            (manual_incident_slug,),
        ).fetchone() == ("manual",)
        with pytest.raises(psycopg.Error, match="incident consolidation policy is immutable"):
            conn.execute(
                "UPDATE incidents SET consolidation_policy = 'managed' WHERE slug = %s",
                (manual_incident_slug,),
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                    INSERT INTO incidents (
                        slug, title, severity, status, started_at, summary,
                        consolidation_policy
                    ) VALUES (%s, 'Invalid', 'sev2', 'open', now(), 'invalid', 'automatic')
                """,
                (f"invalid-{uuid4().hex}",),
            )

        pilot_id = uuid4()
        confirmation_id = uuid4()
        preregistration_sha256 = f"prereg-{uuid4().hex}"
        study_key = f"study-{uuid4().hex}"
        claim_family = f"claim-family-{uuid4().hex}"
        preregistration = {
            "pilot_experiment_id": str(pilot_id),
            "sha256": preregistration_sha256,
        }
        _insert_experiment(
            conn,
            experiment_id=pilot_id,
            kind="pilot",
            status="completed",
            study_key_sha256=study_key,
            claim_family_sha256=claim_family,
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_experiment(
                conn,
                experiment_id=uuid4(),
                kind="pilot",
                status="created",
                study_key_sha256=f"no-op-code-study-{uuid4().hex}",
                claim_family_sha256=claim_family,
            )
        conn.execute(
            """
                INSERT INTO benchmark_confirmation_preregistrations (
                    pilot_experiment_id, preregistration, preregistration_sha256
                ) VALUES (%s, %s, %s)
            """,
            (pilot_id, Jsonb(preregistration), preregistration_sha256),
        )
        _insert_experiment(
            conn,
            experiment_id=confirmation_id,
            kind="confirmation",
            status="created",
            preregistration=preregistration,
            preregistration_sha256=preregistration_sha256,
            study_key_sha256=study_key,
            claim_family_sha256=claim_family,
        )
        assert conn.execute(
            """
                SELECT confirmation_experiment_id, bound_at IS NOT NULL
                FROM benchmark_confirmation_preregistrations
                WHERE pilot_experiment_id = %s
            """,
            (pilot_id,),
        ).fetchone() == (confirmation_id, True)
        assert conn.execute(
            """
                SELECT confirmation_experiment_id, binding_sequence
                FROM benchmark_confirmation_bindings
                WHERE pilot_experiment_id = %s
            """,
            (pilot_id,),
        ).fetchone() == (confirmation_id, 1)
        with pytest.raises(
            psycopg.Error,
            match="benchmark preregistrations permit only verified binding transitions",
        ):
            conn.execute(
                """
                    UPDATE benchmark_confirmation_preregistrations
                    SET preregistration_sha256 = 'mutated'
                    WHERE pilot_experiment_id = %s
                """,
                (pilot_id,),
            )

        conn.execute(
            "UPDATE benchmark_experiments SET status = 'incomplete' WHERE id = %s",
            (confirmation_id,),
        )
        replacement_confirmation_id = uuid4()
        _insert_experiment(
            conn,
            experiment_id=replacement_confirmation_id,
            kind="confirmation",
            status="created",
            preregistration=preregistration,
            preregistration_sha256=preregistration_sha256,
            study_key_sha256=study_key,
            claim_family_sha256=claim_family,
        )
        assert conn.execute(
            """
                SELECT confirmation_experiment_id
                FROM benchmark_confirmation_preregistrations
                WHERE pilot_experiment_id = %s
            """,
            (pilot_id,),
        ).fetchone() == (replacement_confirmation_id,)
        assert {
            (row[0], row[1])
            for row in conn.execute(
                """
                    SELECT confirmation_experiment_id, binding_sequence
                    FROM benchmark_confirmation_bindings
                    WHERE pilot_experiment_id = %s
                """,
                (pilot_id,),
            ).fetchall()
        } == {(confirmation_id, 1), (replacement_confirmation_id, 2)}
        with pytest.raises(
            psycopg.Error,
            match="benchmark confirmation binding history is append-only",
        ):
            conn.execute(
                """
                    UPDATE benchmark_confirmation_bindings
                    SET bound_at = now()
                    WHERE confirmation_experiment_id = %s
                """,
                (confirmation_id,),
            )
        conn.execute(
            "UPDATE benchmark_experiments SET status = 'running' WHERE id = %s",
            (replacement_confirmation_id,),
        )
        conn.execute(
            """
                INSERT INTO benchmark_trials (
                    experiment_id, variant_id, repetition, arm, namespace,
                    status, recovered, action_count, penalized_action_count,
                    unsafe_action_count, completed_at
                ) VALUES (
                    %s, 'outcome', 1, 'consolidated_lesson', 'outcome',
                    'completed', true, 1, 1, 0, now()
                )
            """,
            (replacement_confirmation_id,),
        )
        conn.execute(
            "UPDATE benchmark_experiments SET status = 'incomplete' WHERE id = %s",
            (replacement_confirmation_id,),
        )
        with pytest.raises(
            psycopg.Error,
            match="outcome-bearing incomplete benchmark attempts cannot be replaced",
        ):
            _insert_experiment(
                conn,
                experiment_id=uuid4(),
                kind="confirmation",
                status="created",
                preregistration=preregistration,
                preregistration_sha256=preregistration_sha256,
                study_key_sha256=study_key,
                claim_family_sha256=claim_family,
            )

        with pytest.raises(psycopg.Error):
            _insert_experiment(
                conn,
                experiment_id=uuid4(),
                kind="confirmation",
                status="created",
                preregistration=preregistration,
                preregistration_sha256=preregistration_sha256,
                study_key_sha256=study_key,
            )

        second_pilot_id = uuid4()
        second_claim_family = f"claim-family-{uuid4().hex}"
        second_preregistration_sha256 = f"prereg-{uuid4().hex}"
        second_preregistration = {
            "pilot_experiment_id": str(second_pilot_id),
            "sha256": second_preregistration_sha256,
        }
        _insert_experiment(
            conn,
            experiment_id=second_pilot_id,
            kind="pilot",
            status="completed",
            study_key_sha256=f"study-{uuid4().hex}",
            claim_family_sha256=second_claim_family,
        )
        conn.execute(
            """
                INSERT INTO benchmark_confirmation_preregistrations (
                    pilot_experiment_id, preregistration, preregistration_sha256
                ) VALUES (%s, %s, %s)
            """,
            (
                second_pilot_id,
                Jsonb(second_preregistration),
                second_preregistration_sha256,
            ),
        )
        with pytest.raises(
            psycopg.Error,
            match="direct binding requires an existing matching confirmation",
        ):
            conn.execute(
                """
                    UPDATE benchmark_confirmation_preregistrations
                    SET confirmation_experiment_id = %s, bound_at = now()
                    WHERE pilot_experiment_id = %s
                """,
                (confirmation_id, second_pilot_id),
            )

        incomplete_study_key = f"incomplete-study-{uuid4().hex}"
        incomplete_claim_family = f"incomplete-family-{uuid4().hex}"
        _insert_experiment(
            conn,
            experiment_id=uuid4(),
            kind="pilot",
            status="incomplete",
            study_key_sha256=incomplete_study_key,
            claim_family_sha256=incomplete_claim_family,
        )
        replacement_pilot_id = uuid4()
        _insert_experiment(
            conn,
            experiment_id=replacement_pilot_id,
            kind="pilot",
            status="created",
            study_key_sha256=incomplete_study_key,
            claim_family_sha256=incomplete_claim_family,
        )
        assert conn.execute(
            """
                SELECT count(*) FROM benchmark_experiments
                WHERE study_key_sha256 = %s AND experiment_kind = 'pilot'
            """,
            (incomplete_study_key,),
        ).fetchone() == (2,)
        conn.execute(
            "UPDATE benchmark_experiments SET status = 'running' WHERE id = %s",
            (replacement_pilot_id,),
        )
        conn.execute(
            """
                INSERT INTO benchmark_trials (
                    experiment_id, variant_id, repetition, arm, namespace,
                    status, penalized_action_count
                ) VALUES (%s, 'pilot-outcome', 1, 'no_lesson', 'pilot', 'invalid', 6)
            """,
            (replacement_pilot_id,),
        )
        conn.execute(
            "UPDATE benchmark_experiments SET status = 'incomplete' WHERE id = %s",
            (replacement_pilot_id,),
        )
        with pytest.raises(
            psycopg.Error,
            match="outcome-bearing incomplete benchmark attempts cannot be replaced",
        ):
            _insert_experiment(
                conn,
                experiment_id=uuid4(),
                kind="pilot",
                status="created",
                study_key_sha256=incomplete_study_key,
                claim_family_sha256=incomplete_claim_family,
            )

        action_study_key = f"action-study-{uuid4().hex}"
        action_claim_family = f"action-family-{uuid4().hex}"
        action_experiment_id = uuid4()
        action_trial_id = uuid4()
        action_decision_id = f"benchmark-action-{uuid4()}"
        _insert_experiment(
            conn,
            experiment_id=action_experiment_id,
            kind="pilot",
            status="running",
            study_key_sha256=action_study_key,
            claim_family_sha256=action_claim_family,
        )
        conn.execute(
            """
                INSERT INTO benchmark_trials (
                    id, experiment_id, variant_id, repetition, arm, namespace,
                    status, started_at
                ) VALUES (%s, %s, 'action-only', 1, 'no_lesson', 'action', 'running', now())
            """,
            (action_trial_id, action_experiment_id),
        )
        conn.execute(
            """
                INSERT INTO memory_decisions (
                    id, actor, decision_kind, purpose, namespace
                ) VALUES (%s, 'benchmark.agent', 'memory_retrieval', 'action test', 'action')
            """,
            (action_decision_id,),
        )
        conn.execute(
            """
                INSERT INTO benchmark_actions (
                    trial_id, step, decision_id, action, observation
                ) VALUES (%s, 1, %s, 'stop', '{}'::JSONB)
            """,
            (action_trial_id, action_decision_id),
        )
        conn.execute(
            """
                UPDATE benchmark_trials
                SET status = 'infrastructure_failed', failure_code = 'NetworkError',
                    completed_at = now()
                WHERE id = %s
            """,
            (action_trial_id,),
        )
        conn.execute(
            "UPDATE benchmark_experiments SET status = 'incomplete' WHERE id = %s",
            (action_experiment_id,),
        )
        with pytest.raises(
            psycopg.Error,
            match="outcome-bearing incomplete benchmark attempts cannot be replaced",
        ):
            _insert_experiment(
                conn,
                experiment_id=uuid4(),
                kind="pilot",
                status="created",
                study_key_sha256=action_study_key,
                claim_family_sha256=action_claim_family,
            )

        preparation_experiment_id = uuid4()
        _insert_experiment(
            conn,
            experiment_id=preparation_experiment_id,
            kind="pilot",
            status="created",
            study_key_sha256=f"study-{uuid4().hex}",
            claim_family_sha256=f"claim-family-{uuid4().hex}",
        )
        conn.execute(
            """
                INSERT INTO benchmark_variant_preparations (
                    experiment_id, variant_id, definition_sha256,
                    phase, status, attempt_count, completed_at
                ) VALUES (%s, 'terminal', 'definition', 'complete', 'completed', 1, now())
            """,
            (preparation_experiment_id,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """
                    INSERT INTO benchmark_variant_preparations (
                        experiment_id, variant_id, definition_sha256, attempt_count
                    ) VALUES (%s, 'too-many-attempts', 'definition', 4)
                """,
                (preparation_experiment_id,),
            )
        conn.execute(
            """
                INSERT INTO benchmark_variant_preparations (
                    experiment_id, variant_id, definition_sha256
                ) VALUES (%s, 'parent-terminal', 'definition')
            """,
            (preparation_experiment_id,),
        )
        conn.execute(
            "UPDATE benchmark_experiments SET status = 'completed' WHERE id = %s",
            (preparation_experiment_id,),
        )
        with pytest.raises(
            psycopg.Error,
            match="benchmark variant identity and terminal preparation are immutable",
        ):
            conn.execute(
                """
                    UPDATE benchmark_variant_preparations
                    SET failure_detail = 'late mutation'
                    WHERE experiment_id = %s AND variant_id = 'terminal'
                """,
                (preparation_experiment_id,),
            )
        with pytest.raises(
            psycopg.Error,
            match="benchmark variant identity and terminal preparation are immutable",
        ):
            conn.execute(
                """
                    UPDATE benchmark_variant_preparations
                    SET status = 'leased', attempt_count = 1,
                        lease_owner = 'late', lease_expires_at = now() + INTERVAL '1 minute'
                    WHERE experiment_id = %s AND variant_id = 'parent-terminal'
                """,
                (preparation_experiment_id,),
            )
        with pytest.raises(
            psycopg.Error,
            match="benchmark variant preparation traces cannot be deleted",
        ):
            conn.execute(
                """
                    DELETE FROM benchmark_variant_preparations
                    WHERE experiment_id = %s AND variant_id = 'terminal'
                """,
                (preparation_experiment_id,),
            )
        with pytest.raises(
            psycopg.Error,
            match="benchmark preparations can be inserted only while the experiment is created",
        ):
            conn.execute(
                """
                    INSERT INTO benchmark_variant_preparations (
                        experiment_id, variant_id, definition_sha256
                    ) VALUES (%s, 'late-preparation', 'definition')
                """,
                (preparation_experiment_id,),
            )

        running_experiment_id = uuid4()
        _insert_experiment(
            conn,
            experiment_id=running_experiment_id,
            kind="ci_smoke",
            status="running",
            study_key_sha256=f"study-{uuid4().hex}",
        )
        for arm in ("gold_lesson", "reference_lesson"):
            conn.execute(
                """
                    INSERT INTO benchmark_trials (
                        experiment_id, variant_id, repetition, arm, namespace
                    ) VALUES (%s, %s, 1, %s, 'arm-compatibility')
                """,
                (running_experiment_id, arm, arm),
            )
        conn.execute(
            "UPDATE benchmark_experiments SET status = 'completed' WHERE id = %s",
            (running_experiment_id,),
        )
        with pytest.raises(psycopg.Error, match="benchmark trials can be inserted only"):
            conn.execute(
                """
                    INSERT INTO benchmark_trials (
                        experiment_id, variant_id, repetition, arm, namespace
                    ) VALUES (%s, 'late-trial', 1, 'reference_lesson', 'late')
                """,
                (running_experiment_id,),
            )
        with pytest.raises(
            psycopg.Error,
            match="benchmark trial identity and terminal trace are immutable",
        ):
            conn.execute(
                """
                    UPDATE benchmark_trials
                    SET status = 'completed', recovered = true,
                        action_count = 1, penalized_action_count = 1,
                        unsafe_action_count = 0, completed_at = now()
                    WHERE experiment_id = %s AND variant_id = 'gold_lesson'
                """,
                (running_experiment_id,),
            )
        with pytest.raises(psycopg.Error, match="benchmark trial traces cannot be deleted"):
            conn.execute(
                "DELETE FROM benchmark_trials WHERE experiment_id = %s",
                (running_experiment_id,),
            )


@requires_db
def test_preparation_attempts_commit_and_exhaust_without_attempt_four():
    benchmark_script = _load_benchmark_script()
    target_url = _create_preserved_database("hindsight_benchmark_attempts")
    with psycopg.connect(target_url, autocommit=True) as conn:
        _apply(conn, sorted(MIGRATIONS.glob("[0-9]*.sql")))

        experiment_id = uuid4()
        _insert_experiment(
            conn,
            experiment_id=experiment_id,
            kind="pilot",
            status="created",
            study_key_sha256=f"study-{uuid4().hex}",
            claim_family_sha256=f"claim-family-{uuid4().hex}",
        )
        row = {"variant_id": "provider-retry", "split": "pilot"}
        expected_statuses = ("retrying", "retrying", "infrastructure_failed")
        for expected_attempt, expected_status in enumerate(expected_statuses, start=1):
            claimed, lease_owner = benchmark_script._claim_preparation(
                experiment_id=str(experiment_id),
                row=row,
                db_url=target_url,
            )
            assert lease_owner is not None
            assert claimed["status"] == "leased"
            assert claimed["attempt_count"] == expected_attempt
            assert conn.execute(
                """
                    SELECT status, attempt_count, lease_owner
                    FROM benchmark_variant_preparations
                    WHERE experiment_id = %s AND variant_id = %s
                """,
                (experiment_id, row["variant_id"]),
            ).fetchone() == ("leased", expected_attempt, lease_owner)

            benchmark_script._record_preparation_failure(
                experiment_id=str(experiment_id),
                variant_id=row["variant_id"],
                lease_owner=lease_owner,
                exc=RuntimeError(f"transient failure {expected_attempt}"),
                failure_class="infrastructure",
                db_url=target_url,
            )
            persisted = conn.execute(
                """
                    SELECT status, attempt_count, lease_owner, lease_expires_at,
                        failure_class, completed_at IS NOT NULL
                    FROM benchmark_variant_preparations
                    WHERE experiment_id = %s AND variant_id = %s
                """,
                (experiment_id, row["variant_id"]),
            ).fetchone()
            assert persisted == (
                expected_status,
                expected_attempt,
                None,
                None,
                "infrastructure",
                expected_attempt == 3,
            )

        assert conn.execute(
            "SELECT status, completed_at IS NOT NULL FROM benchmark_experiments WHERE id = %s",
            (experiment_id,),
        ).fetchone() == ("incomplete", True)

        crashed_experiment_id = uuid4()
        _insert_experiment(
            conn,
            experiment_id=crashed_experiment_id,
            kind="pilot",
            status="created",
            study_key_sha256=f"study-{uuid4().hex}",
            claim_family_sha256=f"claim-family-{uuid4().hex}",
        )
        crashed_row = {"variant_id": "expired-lease", "split": "pilot"}
        for expected_attempt in range(1, benchmark_script.MAX_PREPARATION_ATTEMPTS + 1):
            claimed, lease_owner = benchmark_script._claim_preparation(
                experiment_id=str(crashed_experiment_id),
                row=crashed_row,
                db_url=target_url,
            )
            assert lease_owner is not None
            assert claimed["attempt_count"] == expected_attempt
            conn.execute(
                """
                    UPDATE benchmark_variant_preparations
                    SET lease_expires_at = now() - INTERVAL '1 second'
                    WHERE experiment_id = %s AND variant_id = %s
                """,
                (crashed_experiment_id, crashed_row["variant_id"]),
            )

        terminal, lease_owner = benchmark_script._claim_preparation(
            experiment_id=str(crashed_experiment_id),
            row=crashed_row,
            db_url=target_url,
        )
        assert lease_owner is None
        assert terminal["status"] == "infrastructure_failed"
        assert terminal["attempt_count"] == benchmark_script.MAX_PREPARATION_ATTEMPTS
        assert conn.execute(
            "SELECT status FROM benchmark_experiments WHERE id = %s",
            (crashed_experiment_id,),
        ).fetchone() == ("incomplete",)

        scientific_experiment_id = uuid4()
        _insert_experiment(
            conn,
            experiment_id=scientific_experiment_id,
            kind="pilot",
            status="created",
            study_key_sha256=f"study-{uuid4().hex}",
            claim_family_sha256=f"claim-family-{uuid4().hex}",
        )
        scientific_row = {"variant_id": "rank-one-failure", "split": "pilot"}
        claimed, lease_owner = benchmark_script._claim_preparation(
            experiment_id=str(scientific_experiment_id),
            row=scientific_row,
            db_url=target_url,
        )
        assert lease_owner is not None
        benchmark_script._record_preparation_failure(
            experiment_id=str(scientific_experiment_id),
            variant_id=scientific_row["variant_id"],
            lease_owner=lease_owner,
            exc=benchmark_script.ScientificBenchmarkFailure("rank-one retrieval failed"),
            failure_class="scientific",
            db_url=target_url,
        )
        assert conn.execute(
            """
                SELECT preparation.status, preparation.attempt_count,
                    preparation.failure_class, experiment.status
                FROM benchmark_variant_preparations AS preparation
                JOIN benchmark_experiments AS experiment
                    ON experiment.id = preparation.experiment_id
                WHERE preparation.experiment_id = %s
            """,
                (scientific_experiment_id,),
            ).fetchone() == ("scientific_failed", 1, "scientific", "failed")
        assert conn.execute(
            "SELECT count(*) FROM benchmark_trials WHERE experiment_id = %s",
            (scientific_experiment_id,),
        ).fetchone() == (0,)
        assert conn.execute(
            """
                SELECT count(*) FROM benchmark_confirmation_preregistrations
                WHERE pilot_experiment_id = %s
            """,
            (scientific_experiment_id,),
        ).fetchone() == (0,)


@requires_db
def test_interrupted_benchmark_finalizer_closes_children_before_parents():
    benchmark_script = _load_benchmark_script()
    target_url = _create_preserved_database("hindsight_benchmark_finalizer")
    code_sha = f"finalizer-{uuid4().hex}"
    reason = "GitHub runner was interrupted"
    with psycopg.connect(target_url, autocommit=True) as conn:
        _apply(conn, sorted(MIGRATIONS.glob("[0-9]*.sql")))

        created_experiment_id = uuid4()
        running_experiment_id = uuid4()
        _insert_experiment(
            conn,
            experiment_id=created_experiment_id,
            kind="pilot",
            status="created",
            study_key_sha256=f"study-{uuid4().hex}",
            claim_family_sha256=f"claim-family-{uuid4().hex}",
            code_sha=code_sha,
        )
        _insert_experiment(
            conn,
            experiment_id=running_experiment_id,
            kind="pilot",
            status="running",
            study_key_sha256=f"study-{uuid4().hex}",
            claim_family_sha256=f"claim-family-{uuid4().hex}",
            code_sha=code_sha,
        )

        incident_id = conn.execute(
            """
                INSERT INTO incidents (
                    slug, title, severity, status, started_at, summary,
                    consolidation_policy
                ) VALUES (%s, 'Interrupted benchmark', 'sev2', 'open', now(), 'interrupted',
                    'manual')
                RETURNING id
            """,
            (f"interrupted-{uuid4().hex}",),
        ).fetchone()[0]
        consolidation_decision_id = f"consolidation:{uuid4()}"
        conn.execute(
            """
                INSERT INTO memory_decisions (
                    id, actor, decision_kind, purpose, namespace
                ) VALUES (%s, 'consolidation.worker', 'memory_consolidation',
                    'interrupted consolidation', 'benchmark')
            """,
            (consolidation_decision_id,),
        )
        conn.execute(
            """
                INSERT INTO consolidation_jobs (
                    incident_id, decision_id, status
                ) VALUES (%s, %s, 'queued')
            """,
            (incident_id, consolidation_decision_id),
        )
        conn.execute(
            """
                INSERT INTO benchmark_variant_preparations (
                    experiment_id, variant_id, definition_sha256, incident_id
                ) VALUES (%s, 'queued-before-claim', 'definition', %s)
            """,
            (created_experiment_id, incident_id),
        )

        trial_id = uuid4()
        trial_decision_id = f"benchmark:{trial_id}:1"
        conn.execute(
            """
                INSERT INTO benchmark_trials (
                    id, experiment_id, variant_id, repetition, arm, namespace,
                    status, started_at
                ) VALUES (%s, %s, 'interrupted-trial', 1, 'no_lesson', 'benchmark',
                    'running', now())
            """,
            (trial_id, running_experiment_id),
        )
        conn.execute(
            """
                INSERT INTO memory_decisions (
                    id, actor, decision_kind, purpose, namespace
                ) VALUES (%s, 'benchmark.agent', 'memory_retrieval',
                    'interrupted trial decision', 'benchmark')
            """,
            (trial_decision_id,),
        )

        result = benchmark_script.finalize_interrupted_experiments(
            code_sha=code_sha,
            reason=reason,
            db_url=target_url,
        )
        assert set(result["experiment_ids"]) == {
            str(created_experiment_id),
            str(running_experiment_id),
        }
        assert result["code_sha"] == code_sha
        assert {
            key: result[key]
            for key in (
                "experiments",
                "preparations",
                "trials",
                "decisions",
                "consolidation_jobs",
            )
        } == {
            "experiments": 2,
            "preparations": 1,
            "trials": 1,
            "decisions": 2,
            "consolidation_jobs": 1,
        }
        assert conn.execute(
            """
                SELECT status, attempt_count, failure_class, failure_code,
                    completed_at IS NOT NULL
                FROM benchmark_variant_preparations
                WHERE experiment_id = %s
            """,
            (created_experiment_id,),
        ).fetchone() == (
            "infrastructure_failed",
            0,
            "infrastructure",
            "BenchmarkRunnerInterrupted",
            True,
        )
        assert conn.execute(
            """
                SELECT status, failure_code, completed_at IS NOT NULL
                FROM benchmark_trials WHERE id = %s
            """,
            (trial_id,),
        ).fetchone() == ("infrastructure_failed", "BenchmarkRunnerInterrupted", True)
        assert conn.execute(
            """
                SELECT status, error_code, completed_at IS NOT NULL
                FROM consolidation_jobs WHERE incident_id = %s
            """,
            (incident_id,),
        ).fetchone() == ("failed", "BenchmarkRunnerInterrupted", True)
        assert conn.execute(
            """
                SELECT id, status, sealed_at IS NOT NULL
                FROM memory_decisions WHERE id = ANY(%s) ORDER BY id
            """,
            ([consolidation_decision_id, trial_decision_id],),
        ).fetchall() == sorted(
            [
                (consolidation_decision_id, "failed", True),
                (trial_decision_id, "failed", True),
            ]
        )
        assert conn.execute(
            """
                SELECT id, status, completed_at IS NOT NULL
                FROM benchmark_experiments WHERE id = ANY(%s) ORDER BY id
            """,
            ([created_experiment_id, running_experiment_id],),
        ).fetchall() == sorted(
            [
                (created_experiment_id, "incomplete", True),
                (running_experiment_id, "incomplete", True),
            ]
        )

        repeated = benchmark_script.finalize_interrupted_experiments(
            code_sha=code_sha,
            reason=reason,
            db_url=target_url,
        )
        assert repeated["experiment_ids"] == []
        assert all(
            repeated[key] == 0
            for key in (
                "experiments",
                "preparations",
                "trials",
                "decisions",
                "consolidation_jobs",
            )
        )
