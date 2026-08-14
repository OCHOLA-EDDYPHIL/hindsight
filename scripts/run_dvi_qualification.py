"""Qualify the natural tenant vector plan in an exact disposable database."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hindsight.db import database_url, database_url_with_tls_roots  # noqa: E402
from hindsight.embeddings import EMBEDDING_DIMENSIONS, vector_literal  # noqa: E402
from hindsight.vector_index_qualification import (  # noqa: E402
    DVI_CARDINALITIES,
    TENANT_VECTOR_INDEX,
    VectorIndexQualificationError,
    explain_semantic_vector_search,
    finalize_dvi_receipt,
    qualify_semantic_vector_observation,
    qualify_semantic_vector_plan,
    redact_vector_plan,
)

INDEX_MIGRATION = ROOT / "migrations/0030_tenant_vector_cosine_index.sql"
DATABASE_PATTERN = re.compile(r"^hindsight_dvi_[0-9a-f]{12}_[0-9a-f]{12}$")
TENANT_ID = "00000000-0000-0000-0000-000000000401"
OTHER_TENANT_ID = "00000000-0000-0000-0000-000000000402"
NAMESPACE = "dvi-tenant-query"
OTHER_NAMESPACE = "dvi-other-namespace"
PROFILE_ID = "dvi-profile"
OTHER_PROFILE_ID = "dvi-other-profile"


def _admin_and_target_urls(database_name: str) -> tuple[str, str]:
    configured = database_url()
    parts = urlsplit(configured)
    admin = urlunsplit(parts._replace(path="/defaultdb"))
    target = urlunsplit(parts._replace(path=f"/{database_name}"))
    return database_url_with_tls_roots(admin), database_url_with_tls_roots(target)


def _verify_checkout(source_revision: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise RuntimeError("source revision must be an exact lowercase commit SHA")
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    if completed.stdout.strip() != source_revision:
        raise RuntimeError("checked-out revision does not match the requested DVI release")


def _create_database(admin_url: str, database_name: str) -> None:
    if DATABASE_PATTERN.fullmatch(database_name) is None:
        raise RuntimeError("disposable database name is outside the DVI allow-list")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        existing = {
            str(row[0]) for row in admin.execute("SHOW DATABASES").fetchall()
        }
        if database_name in existing:
            raise RuntimeError("disposable DVI database already exists")
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))


def _drop_and_verify_database(admin_url: str, database_name: str) -> bool:
    if DATABASE_PATTERN.fullmatch(database_name) is None:
        raise RuntimeError("refusing to remove a non-DVI database")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} CASCADE").format(sql.Identifier(database_name)))
        existing = {
            str(row[0]) for row in admin.execute("SHOW DATABASES").fetchall()
        }
    return database_name not in existing


def _initialize_exact_vector_schema(conn: psycopg.Connection) -> str:
    migration = INDEX_MIGRATION.read_bytes()
    migration_sha256 = sha256(migration).hexdigest()
    conn.execute(
        """
        CREATE TABLE semantic_memory_vectors (
            tenant_id UUID NOT NULL,
            memory_id UUID NOT NULL,
            profile_id STRING NOT NULL,
            namespace STRING NOT NULL,
            content_digest STRING NOT NULL,
            embedding VECTOR(1024) NOT NULL,
            embedded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (memory_id, profile_id)
        )
        """
    )
    conn.execute(migration.decode("utf-8"))
    index_names = {
        str(row[1]) for row in conn.execute("SHOW INDEXES FROM semantic_memory_vectors")
    }
    if TENANT_VECTOR_INDEX not in index_names:
        raise RuntimeError("exact tenant vector index was not created")
    return migration_sha256


def _copy_rows(conn: psycopg.Connection, rows: list[tuple[str, str, str, str, str, str]]) -> None:
    with conn.cursor().copy(
        "COPY semantic_memory_vectors "
        "(tenant_id, memory_id, profile_id, namespace, content_digest, embedding) "
        "FROM STDIN"
    ) as copy:
        for row in rows:
            copy.write_row(row)


def _qualify_database(
    target_url: str, *, database_name: str, source_revision: str
) -> dict[str, object]:
    target_vector = vector_literal([1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))])
    distractor_vector = vector_literal(
        [0.0, 1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 2))]
    )
    target_memory_id = str(uuid5(NAMESPACE_URL, f"{database_name}:target"))
    attempts: list[dict[str, object]] = []
    inserted = 0
    with psycopg.connect(target_url, autocommit=True) as conn:
        migration_sha256 = _initialize_exact_vector_schema(conn)
        _copy_rows(
            conn,
            [
                (
                    OTHER_TENANT_ID,
                    str(uuid5(NAMESPACE_URL, f"{database_name}:other-tenant")),
                    PROFILE_ID,
                    NAMESPACE,
                    "other-tenant",
                    target_vector,
                ),
                (
                    TENANT_ID,
                    str(uuid5(NAMESPACE_URL, f"{database_name}:other-namespace")),
                    PROFILE_ID,
                    OTHER_NAMESPACE,
                    "other-namespace",
                    target_vector,
                ),
                (
                    TENANT_ID,
                    str(uuid5(NAMESPACE_URL, f"{database_name}:other-profile")),
                    OTHER_PROFILE_ID,
                    NAMESPACE,
                    "other-profile",
                    target_vector,
                ),
            ],
        )
        observation: dict[str, object] | None = None
        for cardinality in DVI_CARDINALITIES:
            rows = []
            for ordinal in range(inserted, cardinality):
                is_target = ordinal == 0
                memory_id = (
                    target_memory_id
                    if is_target
                    else str(uuid5(NAMESPACE_URL, f"{database_name}:distractor:{ordinal}"))
                )
                rows.append(
                    (
                        TENANT_ID,
                        memory_id,
                        PROFILE_ID,
                        NAMESPACE,
                        "target" if is_target else f"distractor-{ordinal}",
                        target_vector if is_target else distractor_vector,
                    )
                )
            _copy_rows(conn, rows)
            inserted = cardinality
            conn.execute("ANALYZE semantic_memory_vectors")
            plan = explain_semantic_vector_search(
                conn,
                tenant_id=TENANT_ID,
                namespace=NAMESPACE,
                profile_id=PROFILE_ID,
                query_vector=[1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))],
                limit=5,
            )
            try:
                qualify_semantic_vector_plan(plan)
            except VectorIndexQualificationError:
                attempts.append(
                    {
                        "same_prefix_cardinality": cardinality,
                        "status": "NOT_SELECTED",
                        "plan_sha256": sha256(plan.encode()).hexdigest(),
                        "redacted_plan": redact_vector_plan(
                            plan,
                            tenant_id=TENANT_ID,
                            namespace=NAMESPACE,
                            profile_id=PROFILE_ID,
                        ),
                    }
                )
                continue
            observation = qualify_semantic_vector_observation(
                conn,
                tenant_id=TENANT_ID,
                namespace=NAMESPACE,
                profile_id=PROFILE_ID,
                query_vector=[1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))],
                expected_memory_id=target_memory_id,
                same_prefix_cardinality=cardinality,
                source_revision=source_revision,
                migration_sha256=migration_sha256,
            )
            attempts.append(
                {
                    "same_prefix_cardinality": cardinality,
                    "status": "PASS",
                    "plan_sha256": observation["plan_sha256"],
                }
            )
            break
        if observation is None:
            raise RuntimeError("natural vector search was not selected at bounded cardinality")
        observation["cardinality_attempts"] = attempts
        observation["total_fixture_rows"] = inserted + 3
        return observation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-revision",
        default=os.environ.get("HINDSIGHT_DEPLOYED_REVISION", ""),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.source_revision:
        raise RuntimeError("--source-revision or HINDSIGHT_DEPLOYED_REVISION is required")
    _verify_checkout(args.source_revision)

    database_name = f"hindsight_dvi_{args.source_revision[:12]}_{uuid4().hex[:12]}"
    admin_url, target_url = _admin_and_target_urls(database_name)
    observation: dict[str, object] | None = None
    cleanup_verified = False
    database_created = False
    try:
        _create_database(admin_url, database_name)
        database_created = True
        observation = _qualify_database(
            target_url,
            database_name=database_name,
            source_revision=args.source_revision,
        )
    finally:
        if database_created:
            cleanup_verified = _drop_and_verify_database(admin_url, database_name)
    if observation is None:
        raise RuntimeError("DVI qualification did not produce an observation")
    receipt = finalize_dvi_receipt(
        observation,
        cleanup_verified=cleanup_verified,
        database_name_sha256=sha256(database_name.encode()).hexdigest(),
    )
    serialized = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(serialized)
    print(serialized, end="")


if __name__ == "__main__":
    main()
