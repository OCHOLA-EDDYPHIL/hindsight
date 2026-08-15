"""Executable CockroachDB acceptance for the infrastructure auditor and DVI."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from hindsight.db import database_url
from hindsight.vector_index_qualification import finalize_dvi_receipt
from scripts.run_dvi_qualification import (
    _admin_and_target_urls,
    _create_database,
    _drop_and_verify_database,
    _qualify_database,
)
from scripts.run_memory_infrastructure_audit import run_denial_probes

requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")
ROOT = Path(__file__).resolve().parents[1]


@requires_db
def test_infrastructure_auditor_executes_reads_but_denies_mutation_ddl_and_grant():
    with psycopg.connect(database_url(), autocommit=True) as conn:
        conn.execute((ROOT / "infra/db/roles.sql").read_text())

    tenant_id = "00000000-0000-0000-0000-000000000001"
    with psycopg.connect(database_url()) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET ROLE hindsight_infrastructure_auditor")
        conn.execute(
            "SELECT set_config('hindsight.tenant_id', %s, true)",
            (tenant_id,),
        )
        assert conn.execute("SELECT count(*) FROM demo_sessions").fetchone() is not None

    receipt = run_denial_probes(db_url=database_url(), tenant_id=tenant_id)

    assert receipt["status"] == "PASS"
    assert {result["id"] for result in receipt["results"]} == {
        "insert",
        "update",
        "delete",
        "ddl",
        "grant",
    }
    serialized = str(receipt)
    assert tenant_id not in serialized
    assert "INSERT INTO" not in serialized


@requires_db
def test_dvi_qualification_uses_exact_index_then_removes_disposable_database():
    source_revision = "a" * 40
    database_name = f"hindsight_dvi_{source_revision[:12]}_{uuid4().hex[:12]}"
    admin_url, target_url = _admin_and_target_urls(database_name)
    observation = None
    cleanup_verified = False
    database_created = False
    try:
        _create_database(admin_url, database_name)
        database_created = True
        observation = _qualify_database(
            target_url,
            database_name=database_name,
            source_revision=source_revision,
        )
    finally:
        if database_created:
            cleanup_verified = _drop_and_verify_database(admin_url, database_name)

    assert observation is not None
    receipt = finalize_dvi_receipt(
        observation,
        cleanup_verified=cleanup_verified,
        database_name_sha256=sha256(database_name.encode()).hexdigest(),
    )
    assert receipt["status"] == "PASS"
    assert receipt["known_neighbor_rank"] == 1
    assert receipt["cleanup"] == {"database_absent": True}
