"""Concurrent semantic-write coverage for side-by-side embedding rotation."""

import os
import threading
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


@requires_db
def test_writes_after_build_snapshot_are_enqueued_before_activation(monkeypatch):
    from hindsight.db import connect, database_url
    from hindsight.embedding_index import (
        EmbeddingCoverageError,
        activate_profile,
        begin_profile_build,
    )
    from hindsight.embeddings import DeterministicEmbeddingProvider
    from hindsight.memory import MemoryStore, Provenance

    class RotationProvider(DeterministicEmbeddingProvider):
        provider_name = "test-rotation"
        model_name = "test-rotation-v1"
        capability = "semantic"
        encoder_revision = "test-rotation-v1"

    class ConflictingProvider(RotationProvider):
        model_name = "test-rotation-v2"
        encoder_revision = "test-rotation-v2"

    original = DeterministicEmbeddingProvider()
    rotation = RotationProvider()
    namespace = f"profile-concurrent-write-{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=original) as store:
        store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="memory present in the initial profile snapshot",
            provenance=Provenance("pytest", "evidence:initial", "seed profile build"),
        )

    building = begin_profile_build(provider=rotation, db_url=database_url())
    with pytest.raises(
        EmbeddingCoverageError,
        match="different embedding profile build is already in progress",
    ):
        begin_profile_build(provider=ConflictingProvider(), db_url=database_url())
    with MemoryStore(url=database_url(), embedding_provider=original) as store:
        embedded_late = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="memory written with the active provider after the build snapshot",
            provenance=Provenance("pytest", "evidence:late", "exercise rotation fence"),
        )
    with MemoryStore(url=database_url()) as store:
        providerless_late = store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="providerless memory written after the build snapshot",
            provenance=Provenance(
                "pytest",
                "evidence:providerless-late",
                "exercise providerless task enqueue",
            ),
        )
    with monkeypatch.context() as legacy_writer:
        legacy_writer.setattr(
            MemoryStore,
            "_enqueue_building_profile_task",
            lambda *_args, **_kwargs: None,
        )
        with MemoryStore(url=database_url(), embedding_provider=original) as store:
            legacy_late = store.remember(
                memory_kind="semantic",
                namespace=namespace,
                content="memory committed by a pre-fence writer after the build snapshot",
                provenance=Provenance(
                    "pytest",
                    "evidence:legacy-late",
                    "exercise deployment catch-up snapshot",
                ),
            )

    with connect() as conn:
        tasks = conn.execute(
            """
                SELECT memory_id, status
                FROM embedding_backfill_tasks
                WHERE profile_id = %s AND memory_id = ANY(%s)
                ORDER BY memory_id
            """,
            (
                building["id"],
                [embedded_late["id"], providerless_late["id"]],
            ),
        ).fetchall()
    assert {str(row[0]) for row in tasks} == {
        str(embedded_late["id"]),
        str(providerless_late["id"]),
    }
    assert {row[1] for row in tasks} == {"pending"}
    with connect() as conn:
        assert conn.execute(
            "SELECT 1 FROM embedding_backfill_tasks "
            "WHERE profile_id = %s AND memory_id = %s",
            (building["id"], legacy_late["id"]),
        ).fetchone() is None

    refreshed = begin_profile_build(provider=rotation, db_url=database_url())
    assert refreshed["id"] == building["id"]
    with connect() as conn:
        assert conn.execute(
            "SELECT status FROM embedding_backfill_tasks "
            "WHERE profile_id = %s AND memory_id = %s",
            (building["id"], legacy_late["id"]),
        ).fetchone() == ("pending",)

    _finish_build(provider=rotation, worker_id="pytest-rotation-new")
    activated = activate_profile(profile_id=str(building["id"]), db_url=database_url())
    assert activated["active_profile_id"] == building["id"]
    assert (
        activate_profile(profile_id=str(building["id"]), db_url=database_url())["generation"]
        == activated["generation"]
    )

    restored = begin_profile_build(provider=original, db_url=database_url())
    _finish_build(provider=original, worker_id="pytest-rotation-restore")
    activate_profile(profile_id=str(restored["id"]), db_url=database_url())


@requires_db
def test_build_fence_blocks_overlapping_write_until_snapshot_commits(monkeypatch):
    import hindsight.memory as memory_module
    from hindsight.db import connect, database_url
    from hindsight.embedding_index import (
        activate_profile,
        begin_profile_build,
        lock_embedding_index_write_fence,
    )
    from hindsight.embeddings import DeterministicEmbeddingProvider, embedding_profile
    from hindsight.memory import MemoryStore, Provenance

    class OverlapProvider(DeterministicEmbeddingProvider):
        provider_name = "test-overlap-rotation"
        model_name = "test-overlap-rotation-v1"
        capability = "semantic"
        encoder_revision = "test-overlap-rotation-v1"

    original = DeterministicEmbeddingProvider()
    rotation = OverlapProvider()
    profile = embedding_profile(rotation)
    namespace = f"profile-overlap-{uuid4()}"
    with MemoryStore(url=database_url(), embedding_provider=original) as store:
        store.remember(
            memory_kind="semantic",
            namespace=namespace,
            content="semantic memory before the overlapping profile build",
            provenance=Provenance("pytest", "evidence:before-overlap", "seed active profile"),
        )
    lock_attempted = threading.Event()
    write_finished = threading.Event()
    errors: list[BaseException] = []
    written: list[dict] = []
    real_write_lock = memory_module.lock_embedding_index_write_fence

    def observed_write_lock(conn):
        lock_attempted.set()
        return real_write_lock(conn)

    monkeypatch.setattr(memory_module, "lock_embedding_index_write_fence", observed_write_lock)

    def write_memory() -> None:
        try:
            with MemoryStore(url=database_url(), embedding_provider=original) as store:
                written.append(
                    store.remember(
                        memory_kind="semantic",
                        namespace=namespace,
                        content="semantic write overlapping the profile snapshot",
                        provenance=Provenance(
                            "pytest",
                            "evidence:overlap",
                            "verify build fence ordering",
                        ),
                    )
                )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            write_finished.set()

    with connect() as build_conn:
        with build_conn.transaction():
            lock_embedding_index_write_fence(build_conn)
            build_conn.execute(
                """
                    INSERT INTO embedding_profiles (
                        id, provider, model, dimensions, capability,
                        encoder_revision, configuration, max_distance, status
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'building')
                    ON CONFLICT (id) DO UPDATE SET
                        status = 'building', retired_at = NULL
                """,
                (
                    profile.profile_id,
                    profile.provider,
                    profile.model,
                    profile.dimensions,
                    profile.capability,
                    profile.encoder_revision,
                    Jsonb(dict(profile.configuration)),
                    profile.max_distance,
                ),
            )
            build_conn.execute(
                """
                    UPDATE embedding_index_state
                    SET building_profile_id = %s, updated_at = now()
                    WHERE singleton = true
                """,
                (profile.profile_id,),
            )
            build_conn.execute(
                """
                    INSERT INTO embedding_backfill_tasks (memory_id, profile_id)
                    SELECT id, %s FROM current_semantic_memories
                    WHERE trust_status = 'active'
                    ON CONFLICT (memory_id, profile_id) DO NOTHING
                """,
                (profile.profile_id,),
            )
            writer = threading.Thread(target=write_memory, daemon=True)
            writer.start()
            assert lock_attempted.wait(timeout=5)
            assert not write_finished.wait(timeout=0.2)

        writer.join(timeout=5)

    assert not writer.is_alive()
    assert errors == []
    assert len(written) == 1
    with connect() as conn:
        task = conn.execute(
            """
                SELECT status FROM embedding_backfill_tasks
                WHERE memory_id = %s AND profile_id = %s
            """,
            (written[0]["id"], profile.profile_id),
        ).fetchone()
    assert task == ("pending",)

    _finish_build(provider=rotation, worker_id="pytest-overlap-new")
    activate_profile(profile_id=profile.profile_id, db_url=database_url())
    restored = begin_profile_build(provider=original, db_url=database_url())
    _finish_build(provider=original, worker_id="pytest-overlap-restore")
    activate_profile(profile_id=str(restored["id"]), db_url=database_url())


def _finish_build(*, provider, worker_id: str) -> None:
    from hindsight.db import database_url
    from hindsight.embedding_index import run_backfill_batch

    while True:
        result = run_backfill_batch(
            provider=provider,
            worker_id=worker_id,
            limit=10_000,
            db_url=database_url(),
        )
        assert result["failed"] == 0
        if result["leased"] == 0:
            return
