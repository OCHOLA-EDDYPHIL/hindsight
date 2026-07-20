"""Authorize, consume, reconcile, and seal frozen learning executions."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.evidence_archive import EvidenceArchive  # noqa: E402
from hindsight.learning_authority import (  # noqa: E402
    PROTOCOL_KEY,
    PROTOCOL_SLOT,
    PRODUCT_PROVENANCE_KEY,
    authorize_execution,
    authorize_protocol,
    canonical_sha256,
    consume_execution,
    execution_authorization_id,
    execution_key,
    protocol_authorization_id,
    reconcile_consumed_execution,
    require_execution_lease,
    seal_execution,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--database-url", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    authorize = subparsers.add_parser("authorize-consume")
    authorize.add_argument("--sequence", type=int, choices=(1, 2), required=True)
    authorize.add_argument("--code-sha", required=True)
    authorize.add_argument("--actor", required=True)
    authorize.add_argument("--workflow-run-id", type=int, required=True)
    authorize.add_argument("--workflow-run-attempt", type=int, required=True)
    authorize.add_argument("--qualification-report", type=pathlib.Path)
    authorize.add_argument("--qualification-receipt", type=pathlib.Path)
    authorize.add_argument("--product-provenance", type=pathlib.Path)

    lease = subparsers.add_parser("require-lease")
    lease.add_argument("--sequence", type=int, choices=(1, 2), required=True)
    lease.add_argument("--code-sha", required=True)
    lease.add_argument("--workflow-run-id", type=int, required=True)
    lease.add_argument("--workflow-run-attempt", type=int, required=True)
    lease.add_argument("--protocol-authorization-sha256", required=True)

    reconcile = subparsers.add_parser("reconcile-consumption")
    reconcile.add_argument("--sequence", type=int, choices=(1, 2), required=True)

    seal = subparsers.add_parser("seal")
    seal.add_argument("--sequence", type=int, choices=(1, 2), required=True)
    seal.add_argument("--report", type=pathlib.Path, required=True)

    args = parser.parse_args()
    archive = EvidenceArchive(
        bucket=args.bucket,
        client=boto3.client("s3", config=aws_client_config()),
    )
    if args.command == "authorize-consume":
        result = _authorize_and_consume(archive=archive, args=args)
    elif args.command == "require-lease":
        result = require_execution_lease(
            db_url=args.database_url,
            sequence=args.sequence,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
            code_sha=args.code_sha,
            protocol_authorization_sha256=args.protocol_authorization_sha256,
        )
    elif args.command == "reconcile-consumption":
        result = reconcile_consumed_execution(
            archive=archive,
            db_url=args.database_url,
            sequence=args.sequence,
        )
    else:
        report = _load_object(args.report)
        result = seal_execution(
            archive=archive,
            db_url=args.database_url,
            sequence=args.sequence,
            study_report=report,
        )
    json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


def _authorize_and_consume(*, archive: EvidenceArchive, args: argparse.Namespace) -> dict[str, Any]:
    if args.sequence == 1:
        for path in (
            args.qualification_report,
            args.qualification_receipt,
            args.product_provenance,
        ):
            if path is None:
                raise ValueError("sequence one requires qualification and product evidence")
        qualification = _load_object(args.qualification_report)
        receipt = _load_object(args.qualification_receipt)
        product = _load_object(args.product_provenance)
        protocol_payload = _protocol_payload(
            archive=archive,
            qualification=qualification,
            qualification_receipt=receipt,
            product_provenance=product,
            code_sha=args.code_sha,
            actor=args.actor,
            workflow_run_id=args.workflow_run_id,
            workflow_run_attempt=args.workflow_run_attempt,
        )
        protocol = authorize_protocol(
            archive=archive,
            db_url=args.database_url,
            payload=protocol_payload,
        )
    else:
        protocol_payload, protocol_record = archive.get_canonical_json(key=PROTOCOL_KEY)
        protocol = {"payload": protocol_payload, "record": protocol_record}
        if protocol_payload.get("code_sha") != args.code_sha:
            raise RuntimeError("sequence two must use the original protocol revision")

    execution_payload = {
        "schema_version": 1,
        "protocol_authorization_id": protocol_authorization_id(),
        "protocol_authorization_sha256": protocol["record"]["sha256"],
        "execution_authorization_id": execution_authorization_id(args.sequence),
        "sequence": args.sequence,
        "authorized_by": args.actor,
        "authorization_workflow_run_id": args.workflow_run_id,
        "authorization_workflow_run_attempt": args.workflow_run_attempt,
    }
    if args.sequence == 2:
        previous = archive.get_canonical_json(
            key=execution_key(1, "finalization")
        )
        execution_payload["previous_finalization_sha256"] = previous[1]["sha256"]
    execution = authorize_execution(
        archive=archive,
        db_url=args.database_url,
        sequence=args.sequence,
        payload=execution_payload,
    )
    consumption = consume_execution(
        archive=archive,
        db_url=args.database_url,
        sequence=args.sequence,
        workflow_run_id=args.workflow_run_id,
        workflow_run_attempt=args.workflow_run_attempt,
        code_sha=args.code_sha,
    )
    return {
        "protocol_authorization_id": protocol_authorization_id(),
        "protocol_authorization_sha256": protocol["record"]["sha256"],
        "execution_authorization_id": execution_authorization_id(args.sequence),
        "execution_authorization_sha256": execution["record"]["sha256"],
        "consumption_sha256": consumption["record"]["sha256"],
        "sequence": args.sequence,
    }


def _protocol_payload(
    *,
    archive: EvidenceArchive,
    qualification: dict[str, Any],
    qualification_receipt: dict[str, Any],
    product_provenance: dict[str, Any],
    code_sha: str,
    actor: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
) -> dict[str, Any]:
    protocol = dict(qualification.get("protocol") or {})
    workflow = dict(qualification.get("workflow") or {})
    profile = dict(qualification.get("profile") or {})
    if (
        qualification.get("status") != "qualified"
        or qualification.get("mode") != "confirmation"
        or qualification.get("code_sha") != code_sha
        or protocol.get("reasoning_provider") != "gemini"
        or protocol.get("reasoning_model") != "gemini-3.1-flash-lite"
        or profile.get("provider") != "gemini"
        or profile.get("model") != "gemini-embedding-2"
        or profile.get("max_distance") != 0.35
        or qualification.get("summary", {}).get("expected_variants") != 12
        or qualification.get("summary", {}).get("all_targets_rank_one") is not True
        or qualification.get("summary", {}).get("all_index_parity") is not True
    ):
        raise ValueError("qualification does not satisfy frozen-v3 authority")
    if (
        int(product_provenance.get("run_id") or 0) <= 0
        or product_provenance.get("head_sha") != code_sha
    ):
        raise ValueError("product provenance does not match the learning revision")
    manifest_payload, manifest_record = archive.get_canonical_json(
        key=str(qualification_receipt.get("manifest_key") or ""),
        version_id=str(qualification_receipt.get("manifest_version_id") or ""),
    )
    objects = dict(manifest_payload.get("objects") or {})
    report_record = dict(objects.get("report") or {})
    if (
        qualification_receipt.get("bucket") != archive.bucket
        or qualification_receipt.get("manifest_sha256") != manifest_record["sha256"]
        or report_record.get("sha256") != canonical_sha256(qualification)
        or int(workflow.get("run_id") or 0) <= 0
        or int(workflow.get("run_attempt") or 0) <= 0
    ):
        raise ValueError("qualification receipt does not bind the supplied report")
    product_record = archive.put_canonical_json(
        key=PRODUCT_PROVENANCE_KEY,
        payload=product_provenance,
    )
    return {
        "schema_version": 1,
        "authorization_slot": PROTOCOL_SLOT,
        "protocol_authorization_id": protocol_authorization_id(),
        "protocol_schema_version": 3,
        "protocol_identity_sha256": qualification["protocol_identity_sha256"],
        "corpus_sha256": qualification["corpus_sha256"],
        "code_sha": code_sha,
        "reasoning_provider": "gemini",
        "reasoning_model": "gemini-3.1-flash-lite",
        "embedding_profile_id": str(profile["id"]),
        "embedding_provider": "gemini",
        "embedding_model": "gemini-embedding-2",
        "embedding_max_distance": 0.35,
        "qualification_run_id": int(workflow["run_id"]),
        "qualification_run_attempt": int(workflow["run_attempt"]),
        "qualification_evidence_sha256": manifest_record["sha256"],
        "product_run_id": int(product_provenance["run_id"]),
        "product_run_attempt": int(product_provenance["run_attempt"]),
        "product_provenance_sha256": product_record["sha256"],
        "product_provenance_archive_key": product_record["key"],
        "product_provenance_archive_version_id": product_record["version_id"],
        "authorized_by": actor,
        "authorization_workflow_run_id": workflow_run_id,
        "authorization_workflow_run_attempt": workflow_run_attempt,
    }


def _load_object(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
