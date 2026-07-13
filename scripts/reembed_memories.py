"""Rebuild semantic-memory vectors for the configured embedding model."""

from __future__ import annotations

import argparse
import pathlib
import sys
from typing import Any

from psycopg.rows import dict_row

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.db import connect  # noqa: E402
from hindsight.embeddings import embedding_provider_from_env, vector_literal  # noqa: E402
from hindsight.runtime import runtime_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--namespace")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    settings = runtime_settings(use_cache=False)
    provider = embedding_provider_from_env(settings.provider_env)
    with connect(
        settings.database_url,
        application_name="hindsight-reembed",
    ) as conn:
        if args.dry_run:
            count = _count_pending(conn, provider=provider, namespace=args.namespace)
            print(
                f"re-embedding: {count} memories require {provider.provider_name}/"
                f"{provider.model_name} ({provider.dimensions} dimensions)"
            )
            return

        processed = 0
        after_id: str | None = None
        while True:
            rows = _pending_batch(
                conn,
                provider=provider,
                namespace=args.namespace,
                after_id=after_id,
                limit=args.batch_size,
            )
            if not rows:
                break
            with conn.transaction():
                for row in rows:
                    embedding = provider.embed(str(row["content"]))
                    conn.execute(
                        """
                            UPSERT INTO semantic_memory_embeddings (
                                memory_id, namespace, embedding, provider,
                                model, dimensions, embedded_at
                            )
                            VALUES (%s, %s, %s::VECTOR(1024), %s, %s, %s, now())
                        """,
                        (
                            row["id"],
                            row["namespace"],
                            vector_literal(embedding, dimensions=provider.dimensions),
                            provider.provider_name,
                            provider.model_name,
                            provider.dimensions,
                        ),
                    )
                    after_id = str(row["id"])
                    processed += 1
            print(f"re-embedding: processed {processed}")
    print(f"re-embedding: complete ({processed} updated)")


def _count_pending(conn: Any, *, provider: Any, namespace: str | None) -> int:
    where, params = _pending_filter(provider=provider, namespace=namespace)
    row = conn.execute(
        f"""
            SELECT count(*)
            FROM semantic_memories AS m
            LEFT JOIN semantic_memory_embeddings AS e ON e.memory_id = m.id
            WHERE {where}
        """,
        params,
    ).fetchone()
    return int(row[0])


def _pending_batch(
    conn: Any,
    *,
    provider: Any,
    namespace: str | None,
    after_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    where, params = _pending_filter(provider=provider, namespace=namespace)
    if after_id:
        where += " AND m.id > %s"
        params.append(after_id)
    params.append(limit)
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            f"""
                SELECT m.id, m.namespace, m.content
                FROM semantic_memories AS m
                LEFT JOIN semantic_memory_embeddings AS e ON e.memory_id = m.id
                WHERE {where}
                ORDER BY m.id
                LIMIT %s
            """,
            params,
        )
        return [dict(row) for row in cursor.fetchall()]


def _pending_filter(*, provider: Any, namespace: str | None) -> tuple[str, list[Any]]:
    conditions = [
        "(e.memory_id IS NULL OR e.provider != %s OR e.model != %s OR e.dimensions != %s)"
    ]
    params: list[Any] = [provider.provider_name, provider.model_name, provider.dimensions]
    if namespace:
        conditions.append("m.namespace = %s")
        params.append(namespace)
    return " AND ".join(conditions), params


if __name__ == "__main__":
    main()
