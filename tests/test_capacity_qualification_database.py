from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import psycopg
import pytest


ROOT = Path(__file__).resolve().parents[1]
requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


def _producer():
    path = ROOT / "scripts" / "run_capacity_qualification.py"
    spec = importlib.util.spec_from_file_location("run_capacity_qualification", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@requires_db
def test_capacity_seed_staging_executes_on_cockroach_and_partitions_batches():
    producer = _producer()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS capacity_seed")
        conn.execute("DROP TABLE IF EXISTS capacity_tenant_seed")
        try:
            producer._create_seed_staging(conn, producer.Deadline.after(10))
            producer._insert_tenant_staging(conn, run_id="abcdefgh", tenant_number=1)
            producer._insert_seed_staging(conn, tenant_number=1, row_count=3)
            producer._insert_seed_staging(conn, tenant_number=2, row_count=2)
            rows = conn.execute(
                "SELECT tenant_number, count(*)::INT8, "
                "min(sha256(('capacity:' || ordinal::STRING)::BYTES)) "
                "FROM capacity_seed "
                "GROUP BY tenant_number ORDER BY tenant_number"
            ).fetchall()
            assert [(row[0], row[1]) for row in rows] == [(1, 3), (2, 2)]
            assert all(len(row[2]) == 64 for row in rows)
            tenant = conn.execute(
                "SELECT tenant_number, namespace, embedding::STRING FROM capacity_tenant_seed"
            ).fetchone()
            assert tenant[0:2] == (1, "capacity.synthetic.01")
            assert len(tenant[2].removeprefix("[").removesuffix("]").split(",")) == 1024
        finally:
            conn.execute("DROP TABLE IF EXISTS capacity_seed")
            conn.execute("DROP TABLE IF EXISTS capacity_tenant_seed")
        assert conn.execute("SELECT to_regclass('capacity_seed')").fetchone() == (None,)
        assert conn.execute("SELECT to_regclass('capacity_tenant_seed')").fetchone() == (None,)
