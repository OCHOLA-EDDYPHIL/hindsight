"""Run the pinned, read-only governed-memory infrastructure audit."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hindsight.db import database_url  # noqa: E402
from hindsight.infrastructure_auditor import run_infrastructure_audit  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument(
        "--source-revision",
        default=os.environ.get("HINDSIGHT_DEPLOYED_REVISION", ""),
        required=False,
    )
    parser.add_argument("--repeat", type=int, choices=(1, 2), default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.source_revision:
        raise RuntimeError("--source-revision or HINDSIGHT_DEPLOYED_REVISION is required")

    receipts: list[dict[str, object]] = []
    for _ in range(args.repeat):
        with psycopg.connect(database_url()) as conn:
            receipts.append(
                run_infrastructure_audit(
                    conn,
                    tenant_id=args.tenant_id,
                    namespace=args.namespace,
                    source_revision=args.source_revision,
                )
            )
    conclusions = {str(receipt["conclusion_sha256"]) for receipt in receipts}
    if len(conclusions) != 1:
        raise RuntimeError("repeated infrastructure audits produced different conclusions")
    if any(receipt["status"] == "FAIL" for receipt in receipts):
        raise RuntimeError("infrastructure audit failed")
    document = {
        "schema_version": "hindsight.infrastructure-audit-run.v1",
        "repeat_count": args.repeat,
        "conclusion_sha256": receipts[0]["conclusion_sha256"],
        "receipts": receipts,
    }
    serialized = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(serialized)
    print(serialized, end="")


if __name__ == "__main__":
    main()
