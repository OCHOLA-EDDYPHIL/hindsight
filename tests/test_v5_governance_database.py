"""CockroachDB precision characterization for V2 governance."""

from __future__ import annotations

import os
import random

import pytest

from hindsight.db import connect, database_url
from hindsight.embeddings import vector_literal
from hindsight.v5_governance import V2_MAXIMUM_DISTANCE_DELTA, _cosine_distance


requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


@requires_db
def test_stored_vector_cosine_distance_stays_within_v2_precision_tolerance() -> None:
    generator = random.Random(2684)
    query = [generator.uniform(-1.0, 1.0) for _ in range(1024)]
    document = [0.7 * value + 0.3 * generator.uniform(-1.0, 1.0) for value in query]
    direct = _cosine_distance(query, document)

    with connect(database_url(), application_name="hindsight-v5-precision-test") as connection:
        row = connection.execute(
            """
                WITH stored (embedding) AS (VALUES (%s::VECTOR(1024)))
                SELECT embedding <=> %s::VECTOR(1024) FROM stored
            """,
            (vector_literal(document), vector_literal(query)),
        ).fetchone()

    assert row is not None
    assert abs(direct - float(row[0])) <= V2_MAXIMUM_DISTANCE_DELTA
