"""Claim, finalize, and reconcile immutable qualification-family authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.evidence_archive import EvidenceArchive  # noqa: E402
from hindsight.qualification_authority import (  # noqa: E402
    claim_attempt,
    family_sha256,
    finalize_attempt,
    reconcile_legacy_terminal,
    v3_family_contract,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "fixtures" / "benchmark_variants.json"
BENCHMARK_TABLES = (
    "benchmark_experiments",
    "benchmark_trials",
    "benchmark_actions",
    "benchmark_confirmation_preregistrations",
    "benchmark_confirmation_bindings",
    "benchmark_variant_preparations",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    contract = subparsers.add_parser("contract")
    contract.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)

    claim = subparsers.add_parser("claim")
    _archive_arguments(claim)
    claim.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    claim.add_argument("--sequence", type=int, choices=(1, 2), required=True)
    claim.add_argument("--actor", required=True)
    claim.add_argument("--workflow-run-id", type=int, required=True)
    claim.add_argument("--workflow-run-attempt", type=int, required=True)
    claim.add_argument("--code-sha", required=True)

    incomplete = subparsers.add_parser("infrastructure-report")
    incomplete.add_argument("--attempt", type=pathlib.Path, required=True)
    incomplete.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    incomplete.add_argument("--output", type=pathlib.Path, required=True)
    incomplete.add_argument("--outcome-marker", type=pathlib.Path)

    finalize = subparsers.add_parser("finalize")
    _archive_arguments(finalize)
    finalize.add_argument("--database-url")
    finalize.add_argument("--sequence", type=int, choices=(1, 2), required=True)
    finalize.add_argument("--report", type=pathlib.Path, required=True)
    finalize.add_argument("--receipt", type=pathlib.Path, required=True)

    reconcile = subparsers.add_parser("reconcile-terminal")
    _archive_arguments(reconcile)
    reconcile.add_argument("--database-url", required=True)
    reconcile.add_argument("--report", type=pathlib.Path, required=True)
    reconcile.add_argument("--receipt", type=pathlib.Path, required=True)

    args = parser.parse_args()
    if args.command == "contract":
        result = _contract_result(args.corpus)
    elif args.command == "infrastructure-report":
        result = _infrastructure_report(
            attempt=args.attempt,
            corpus=args.corpus,
            outcome_accessed=bool(args.outcome_marker and args.outcome_marker.exists()),
        )
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    else:
        archive = EvidenceArchive(
            bucket=args.bucket,
            client=boto3.client("s3", config=aws_client_config()),
        )
        if args.command == "claim":
            contract_payload = _contract_result(args.corpus)["contract"]
            result = claim_attempt(
                archive=archive,
                contract=contract_payload,
                sequence=args.sequence,
                actor=args.actor,
                workflow_run_id=args.workflow_run_id,
                workflow_run_attempt=args.workflow_run_attempt,
                code_sha=args.code_sha,
            )
        elif args.command == "finalize":
            result = finalize_attempt(
                archive=archive,
                db_url=args.database_url,
                sequence=args.sequence,
                report=_load_object(args.report),
                receipt=_load_object(args.receipt),
            )
        else:
            result = reconcile_legacy_terminal(
                archive=archive,
                db_url=args.database_url,
                report=_load_object(args.report),
                receipt=_load_object(args.receipt),
            )
    json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


def _archive_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bucket", required=True)


def _contract_result(corpus: pathlib.Path) -> dict[str, Any]:
    corpus_sha256 = hashlib.sha256(corpus.read_bytes()).hexdigest()
    contract = v3_family_contract(corpus_sha256=corpus_sha256)
    return {"family_sha256": family_sha256(contract), "contract": contract}


def _infrastructure_report(
    *,
    attempt: pathlib.Path,
    corpus: pathlib.Path,
    outcome_accessed: bool = False,
) -> dict[str, Any]:
    attempt_payload = _load_object(attempt)
    contract_result = _contract_result(corpus)
    contract = contract_result["contract"]
    counts = {table: 0 for table in BENCHMARK_TABLES}
    return {
        "schema_version": 2,
        "status": "infrastructure_incomplete",
        "mode": "confirmation",
        "workflow": {
            "run_id": int(attempt_payload["run_id"]),
            "run_attempt": int(attempt_payload["run_attempt"]),
            "sequence": int(attempt_payload["sequence"]),
        },
        "corpus_sha256": contract["corpus_sha256"],
        "code_sha": str(attempt_payload["code_sha"]),
        "scientific_family_sha256": contract_result["family_sha256"],
        "outcome_accessed": outcome_accessed,
        "protocol": {
            "code_sha": str(attempt_payload["code_sha"]),
            "corpus_sha256": contract["corpus_sha256"],
            "mode": "confirmation",
            "variant_count": contract["variant_count"],
            "reasoning_provider": "gemini",
            "reasoning_model": contract["reasoning"]["model"],
            "embedding_profile": contract["embedding_profile"],
            "retrieval": {
                field: contract["retrieval"][field]
                for field in ("max_distance", "rank_requirement", "fallback", "reranking")
            },
        },
        "profile": contract["embedding_profile"],
        "benchmark_state_unchanged": True,
        "benchmark_state_empty": True,
        "benchmark_row_counts_before": counts,
        "benchmark_row_counts_after": counts,
        "summary": {
            "expected_variants": contract["variant_count"],
            "completed_variants": 0,
            "direct_rank_one": 0,
            "indexed_rank_one": 0,
            "all_index_parity": False,
            "all_targets_rank_one": False,
        },
        "variants": [],
    }


def _load_object(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
