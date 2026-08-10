"""Evaluate hosted retrieval rankings against sanitized relevance judgments."""

from __future__ import annotations

import argparse
import json
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def evaluate(fixture: dict[str, Any], results: dict[str, Any], *, source_revision: str) -> dict[str, Any]:
    if fixture.get("sanitized") is not True:
        raise ValueError("retrieval fixture must be explicitly sanitized")
    expected = {row["query_id"]: set(row["relevant_memory_ids"]) for row in fixture["cases"]}
    observed = {row["query_id"]: list(row["memory_ids"]) for row in results["cases"]}
    if set(expected) != set(observed):
        raise ValueError("result query identities must exactly match the fixture")
    raw = []
    reciprocal_ranks = []
    hits = 0
    for query_id in sorted(expected):
        ranking = observed[query_id]
        rank = next((index for index, value in enumerate(ranking, 1) if value in expected[query_id]), None)
        hits += int(rank is not None)
        reciprocal_ranks.append(0.0 if rank is None else 1.0 / rank)
        raw.append({"query_id": query_id, "returned_memory_ids": ranking, "first_relevant_rank": rank})
    count = len(raw)
    return {
        "schema_version": 1,
        "kind": "retrieval_quality_evidence",
        "source_revision": source_revision,
        "measured_at": datetime.now(UTC).isoformat(),
        "method": {"fixture_schema": fixture["schema_version"], "metrics": ["recall_at_k", "mrr"]},
        "environment": {"python": platform.python_version(), **results.get("environment", {})},
        "measurements": {"case_count": count, "recall_at_k": hits / count, "mrr": sum(reciprocal_ranks) / count},
        "raw_measurements": raw,
        "limitations": [
            "Sanitized relevance judgments cover only the included incident cases.",
            "These measurements are benchmark evidence and are not a production SLO."
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(json.loads(args.fixture.read_text()), json.loads(args.results.read_text()), source_revision=args.source_revision)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
