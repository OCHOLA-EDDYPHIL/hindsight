"""Side-by-side semantic embedding profile build and activation."""

from __future__ import annotations

import os
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.db import connect, database_url
from hindsight.embeddings import EmbeddingProvider, embedding_profile, vector_literal
from hindsight.security import safe_error_detail


class EmbeddingCoverageError(RuntimeError):
    """Raised when a profile cannot safely become active."""


def begin_profile_build(
    *,
    provider: EmbeddingProvider,
    max_distance: float | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Register a building profile and enqueue every current semantic version."""

    profile = embedding_profile(provider, max_distance=max_distance)
    with connect(db_url, application_name="hindsight-embedding-index") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        INSERT INTO embedding_profiles (
                            id, provider, model, dimensions, capability,
                            encoder_revision, configuration, max_distance, status
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'building')
                        ON CONFLICT (id) DO UPDATE SET
                            max_distance = excluded.max_distance,
                            status = CASE WHEN embedding_profiles.status = 'active'
                                          THEN 'active' ELSE 'building' END
                        RETURNING *
                    """,
                    (
                        profile.profile_id,
                        profile.provider,
                        profile.model,
                        profile.dimensions,
                        profile.capability,
                        profile.encoder_revision,
                        Jsonb(dict(profile.configuration)),
                        max_distance,
                    ),
                )
                row = dict(cur.fetchone())
                cur.execute(
                    """
                        UPDATE embedding_index_state
                        SET building_profile_id = %s, updated_at = now()
                        WHERE singleton = true
                    """,
                    (profile.profile_id,),
                )
                cur.execute(
                    """
                        INSERT INTO embedding_backfill_tasks (memory_id, profile_id)
                        SELECT memory.id, %s
                        FROM current_semantic_memories AS memory
                        WHERE memory.trust_status = 'active'
                        ON CONFLICT (memory_id, profile_id) DO NOTHING
                    """,
                    (profile.profile_id,),
                )
                return row


def run_backfill_batch(
    *,
    provider: EmbeddingProvider,
    worker_id: str,
    limit: int = 25,
    max_distance: float | None = None,
    db_url: str | None = None,
) -> dict[str, int]:
    """Lease and embed a bounded batch without provider calls in a DB transaction."""

    if limit < 1:
        raise ValueError("limit must be at least 1")
    resolved_url = db_url or database_url()
    profile = embedding_profile(provider, max_distance=max_distance)
    with connect(resolved_url, application_name="hindsight-embedding-index") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        SELECT building_profile_id FROM embedding_index_state
                        WHERE singleton = true FOR UPDATE
                    """
                )
                state = cur.fetchone()
                if state is None or state["building_profile_id"] != profile.profile_id:
                    raise EmbeddingCoverageError("provider does not match building profile")
                cur.execute(
                    """
                        SELECT task.memory_id, memory.namespace, memory.content,
                               memory.payload_digest
                        FROM embedding_backfill_tasks AS task
                        JOIN semantic_memories AS memory ON memory.id = task.memory_id
                        WHERE task.profile_id = %s
                            AND task.status IN ('pending', 'retrying', 'leased')
                            AND (task.lease_expires_at IS NULL OR task.lease_expires_at < now()
                                 OR task.lease_owner = %s)
                        ORDER BY task.created_at, task.memory_id
                        LIMIT %s
                        FOR UPDATE OF task
                    """,
                    (profile.profile_id, worker_id, limit),
                )
                tasks = [dict(row) for row in cur.fetchall()]
                for task in tasks:
                    cur.execute(
                        """
                            UPDATE embedding_backfill_tasks
                            SET status = 'leased', lease_owner = %s,
                                lease_expires_at = now() + INTERVAL '2 minutes',
                                attempt_count = attempt_count + 1, updated_at = now()
                            WHERE memory_id = %s AND profile_id = %s
                        """,
                        (worker_id, task["memory_id"], profile.profile_id),
                    )
    completed = 0
    failed = 0
    for task in tasks:
        try:
            embedding = provider.embed_document(task["content"])
            with connect(resolved_url, application_name="hindsight-embedding-index") as conn:
                with conn.transaction():
                    conn.execute(
                        f"""
                            INSERT INTO semantic_memory_vectors (
                                memory_id, profile_id, namespace, content_digest, embedding
                            ) VALUES (%s, %s, %s, %s, %s::VECTOR({provider.dimensions}))
                            ON CONFLICT (memory_id, profile_id) DO UPDATE SET
                                namespace = excluded.namespace,
                                content_digest = excluded.content_digest,
                                embedding = excluded.embedding,
                                embedded_at = now()
                        """,
                        (
                            task["memory_id"],
                            profile.profile_id,
                            task["namespace"],
                            task["payload_digest"],
                            vector_literal(embedding, dimensions=provider.dimensions),
                        ),
                    )
                    conn.execute(
                        """
                            UPDATE embedding_backfill_tasks
                            SET status = 'completed', completed_at = now(), updated_at = now(),
                                lease_expires_at = NULL, error_code = NULL, error_detail = NULL
                            WHERE memory_id = %s AND profile_id = %s AND lease_owner = %s
                        """,
                        (task["memory_id"], profile.profile_id, worker_id),
                    )
            completed += 1
        except Exception as exc:
            failed += 1
            with connect(resolved_url, application_name="hindsight-embedding-index") as conn:
                with conn.transaction():
                    conn.execute(
                        """
                            UPDATE embedding_backfill_tasks
                            SET status = CASE WHEN attempt_count < 3 THEN 'retrying' ELSE 'failed' END,
                                error_code = %s, error_detail = %s, lease_expires_at = NULL,
                                updated_at = now()
                            WHERE memory_id = %s AND profile_id = %s AND lease_owner = %s
                        """,
                        (
                            type(exc).__name__,
                            safe_error_detail(exc, max_chars=1000),
                            task["memory_id"],
                            profile.profile_id,
                            worker_id,
                        ),
                    )
    return {"leased": len(tasks), "completed": completed, "failed": failed}


def activate_profile(*, profile_id: str, db_url: str | None = None) -> dict[str, Any]:
    """Atomically activate only a complete, failure-free current-memory index."""

    with connect(db_url, application_name="hindsight-embedding-index") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM embedding_profiles WHERE id = %s FOR UPDATE", (profile_id,))
                profile = cur.fetchone()
                if profile is None:
                    raise LookupError(profile_id)
                if profile["capability"] == "lexical_hash" and os.environ.get(
                    "AWS_LAMBDA_FUNCTION_NAME"
                ):
                    raise EmbeddingCoverageError("hosted runtime cannot activate lexical hashing")
                cur.execute(
                    """
                        SELECT count(*) AS missing
                        FROM current_semantic_memories AS memory
                        LEFT JOIN semantic_memory_vectors AS vector
                            ON vector.memory_id = memory.id AND vector.profile_id = %s
                        WHERE memory.trust_status = 'active' AND vector.memory_id IS NULL
                    """,
                    (profile_id,),
                )
                missing = int(cur.fetchone()["missing"])
                cur.execute(
                    """
                        SELECT count(*) AS failed FROM embedding_backfill_tasks
                        WHERE profile_id = %s AND status = 'failed'
                    """,
                    (profile_id,),
                )
                failed = int(cur.fetchone()["failed"])
                if missing or failed:
                    raise EmbeddingCoverageError(
                        f"profile coverage incomplete: missing={missing}, failed={failed}"
                    )
                cur.execute(
                    """
                        UPDATE embedding_profiles
                        SET status = 'retired', retired_at = now()
                        WHERE status = 'active' AND id != %s
                    """,
                    (profile_id,),
                )
                cur.execute(
                    """
                        UPDATE embedding_profiles
                        SET status = 'active', activated_at = COALESCE(activated_at, now()),
                            retired_at = NULL
                        WHERE id = %s
                    """,
                    (profile_id,),
                )
                cur.execute(
                    """
                        UPDATE embedding_index_state
                        SET active_profile_id = %s, building_profile_id = NULL,
                            generation = generation + 1, updated_at = now()
                        WHERE singleton = true
                        RETURNING *
                    """,
                    (profile_id,),
                )
                return dict(cur.fetchone())
