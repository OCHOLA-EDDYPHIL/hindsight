"""Qualify the supported tenant-scoped semantic vector query plan."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hindsight.db import connect  # noqa: E402
from hindsight.embeddings import EMBEDDING_DIMENSIONS  # noqa: E402
from hindsight.vector_index_qualification import (  # noqa: E402
    explain_semantic_vector_search,
    qualify_semantic_vector_plan,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    query_vector = [1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))]
    with connect(tenant_id=args.tenant_id) as conn:
        plan = explain_semantic_vector_search(
            conn,
            tenant_id=args.tenant_id,
            namespace=args.namespace,
            profile_id=args.profile_id,
            query_vector=query_vector,
            limit=args.limit,
        )
    qualify_semantic_vector_plan(plan)
    print(plan)


if __name__ == "__main__":
    main()
