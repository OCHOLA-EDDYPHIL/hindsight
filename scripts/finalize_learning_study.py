"""Fence an authorized learning execution and write its scientific classification."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.learning_result import finalize_and_classify  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--sequence", type=int, choices=(1, 2), required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--workflow-run-attempt", type=int, required=True)
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--protocol-authorization-sha256", required=True)
    parser.add_argument("--interruption-reason", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    report = finalize_and_classify(
        db_url=args.database_url,
        sequence=args.sequence,
        workflow_run_id=args.workflow_run_id,
        workflow_run_attempt=args.workflow_run_attempt,
        code_sha=args.code_sha,
        protocol_authorization_sha256=args.protocol_authorization_sha256,
        interruption_reason=args.interruption_reason,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(report["result"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
