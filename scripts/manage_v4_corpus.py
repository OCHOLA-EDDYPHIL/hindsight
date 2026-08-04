"""Construct, review, seal, and split the protected v4 learning corpus."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any
from uuid import uuid4

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.evidence_archive import EvidenceArchive  # noqa: E402
from hindsight.v4_corpus import (  # noqa: E402
    BedrockJsonModel,
    DRAFTER_MODEL,
    ADJUDICATOR_MODELS,
    build_review_packet,
    build_study_manifest,
    construct_pool,
    construction_protocol,
    finalize_review,
    load_sealed_reviewed_pool,
    new_review_state,
    protocol_sha256,
    put_protected_json,
    split_reviewed_pool,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_V3_CORPUS = ROOT / "fixtures" / "benchmark_variants.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("protocol")

    construct = subparsers.add_parser("construct")
    construct.add_argument("--pool-id")
    construct.add_argument("--v3-corpus", type=pathlib.Path, default=DEFAULT_V3_CORPUS)
    construct.add_argument("--output", type=pathlib.Path, required=True)

    prepare = subparsers.add_parser("prepare-review")
    prepare.add_argument("--pool", type=pathlib.Path, required=True)
    prepare.add_argument("--packet", type=pathlib.Path, required=True)
    prepare.add_argument("--state", type=pathlib.Path, required=True)

    review = subparsers.add_parser("finalize-review")
    review.add_argument("--pool", type=pathlib.Path, required=True)
    review.add_argument("--packet", type=pathlib.Path, required=True)
    review.add_argument("--state", type=pathlib.Path, required=True)
    review.add_argument("--output", type=pathlib.Path, required=True)

    download = subparsers.add_parser("download-construction")
    download.add_argument("--bucket", required=True)
    download.add_argument("--receipt", type=pathlib.Path, required=True)
    download.add_argument("--output", type=pathlib.Path, required=True)

    seal = subparsers.add_parser("seal-reviewed-pool")
    seal.add_argument("--reviewed-pool", type=pathlib.Path, required=True)
    seal.add_argument("--bucket", required=True)
    seal.add_argument("--evidence-id", required=True)
    seal.add_argument("--receipt", type=pathlib.Path, required=True)

    split = subparsers.add_parser("split")
    split.add_argument("--bucket", required=True)
    split.add_argument("--receipt", type=pathlib.Path, required=True)
    split.add_argument("--beacon", type=pathlib.Path, required=True)
    split.add_argument("--kms-key-id", required=True)
    split.add_argument("--development-output", type=pathlib.Path, required=True)
    split.add_argument("--split-receipt", type=pathlib.Path, required=True)

    freeze = subparsers.add_parser("freeze-study")
    freeze.add_argument("--code-sha", required=True)
    freeze.add_argument("--split-receipt", type=pathlib.Path, required=True)
    freeze.add_argument("--representation-selection", type=pathlib.Path, required=True)
    freeze.add_argument("--output", type=pathlib.Path, required=True)

    args = parser.parse_args()
    if args.command == "protocol":
        _print_json({"protocol": construction_protocol(), "protocol_sha256": protocol_sha256()})
        return 0
    if args.command == "construct":
        _require_private_path(args.output)
        session = boto3.session.Session()
        _preflight_bedrock(session)
        drafter = BedrockJsonModel.create(
            model_id=DRAFTER_MODEL,
            max_tokens=1800,
            temperature=0.2,
            client_factory=session.client,
        )
        adjudicators = tuple(
            BedrockJsonModel.create(
                model_id=model_id,
                max_tokens=1200,
                temperature=0.0,
                client_factory=session.client,
            )
            for model_id in ADJUDICATOR_MODELS
        )
        pool = construct_pool(
            pool_id=str(args.pool_id or uuid4()),
            drafter=drafter,
            adjudicators=adjudicators,
            v3_corpus=_load_json(args.v3_corpus),
        )
        _write_private_json(args.output, pool)
        _print_json(
            {
                "pool_sha256": pool["pool_sha256"],
                "selected_items": len(pool["items"]),
                "slot_records": len(pool["slot_audit"]),
            }
        )
        return 0
    if args.command == "prepare-review":
        for path in (args.pool, args.packet, args.state):
            _require_private_path(path)
        pool = _load_json(args.pool)
        packet = build_review_packet(pool=pool)
        state = new_review_state(packet=packet)
        _write_private_json(args.packet, packet)
        _write_private_json(args.state, state)
        _print_json(
            {
                "pool_sha256": pool["pool_sha256"],
                "review_items": len(packet["items"]),
                "review_packet_sha256": packet["review_packet_sha256"],
            }
        )
        return 0
    if args.command == "finalize-review":
        for path in (args.pool, args.packet, args.state, args.output):
            _require_private_path(path)
        reviewed = finalize_review(
            pool=_load_json(args.pool),
            packet=_load_json(args.packet),
            state=_load_json(args.state),
        )
        _write_private_json(args.output, reviewed)
        _print_json({"reviewed_pool_sha256": reviewed["reviewed_pool_sha256"]})
        return 0
    if args.command == "freeze-study":
        for path in (args.split_receipt, args.representation_selection, args.output):
            _require_private_path(path)
        manifest = build_study_manifest(
            code_sha=args.code_sha,
            split_receipt=_load_json(args.split_receipt),
            representation_selection=_load_json(args.representation_selection),
        )
        _write_private_json(args.output, manifest)
        _print_json({"manifest_sha256": manifest["manifest_sha256"]})
        return 0

    s3 = boto3.client("s3", config=aws_client_config(read_timeout=60, max_attempts=5))
    archive = EvidenceArchive(bucket=args.bucket, client=s3)
    if args.command == "download-construction":
        _require_private_path(args.receipt)
        _require_private_path(args.output)
        receipt = _load_json(args.receipt)
        manifest, manifest_record = archive.get_canonical_json(
            key=str(receipt.get("manifest_key") or ""),
            version_id=str(receipt.get("manifest_version_id") or ""),
        )
        if receipt.get("manifest_sha256") != manifest_record["sha256"]:
            raise ValueError("construction receipt differs from its archive manifest")
        pool_record = dict((manifest.get("objects") or {}).get("construction_pool") or {})
        pool, observed = archive.get_canonical_json(
            key=str(pool_record.get("key") or ""),
            version_id=str(pool_record.get("version_id") or ""),
        )
        if observed["sha256"] != pool_record.get("sha256"):
            raise ValueError("construction pool differs from its sealed manifest")
        _write_private_json(args.output, pool)
        _print_json({"pool_sha256": pool["pool_sha256"], "selected_items": len(pool["items"])})
        return 0
    if args.command == "seal-reviewed-pool":
        _require_private_path(args.reviewed_pool)
        _require_private_path(args.receipt)
        reviewed = _load_json(args.reviewed_pool)
        receipt = archive.seal_bundle(
            evidence_id=args.evidence_id,
            objects={
                "reviewed_pool": reviewed,
                "construction_protocol": construction_protocol(),
            },
            dependencies={"protocol_sha256": protocol_sha256()},
        )
        _write_private_json(args.receipt, receipt)
        _print_json(
            {
                "manifest_key": receipt["manifest_key"],
                "manifest_sha256": receipt["manifest_sha256"],
            }
        )
        return 0

    _require_private_path(args.receipt)
    _require_private_path(args.beacon)
    _require_private_path(args.development_output)
    _require_private_path(args.split_receipt)
    reviewed, sealed_at = load_sealed_reviewed_pool(
        archive=archive,
        receipt=_load_json(args.receipt),
    )
    split_result = split_reviewed_pool(
        reviewed_pool=reviewed,
        sealed_manifest_sha256=_load_json(args.receipt)["manifest_sha256"],
        sealed_at=sealed_at,
        beacon=_load_json(args.beacon),
    )
    protected_prefix = f"learning/protected-corpora/v4/{split_result['reviewed_pool_sha256']}"
    protected = {
        name: put_protected_json(
            client=s3,
            bucket=args.bucket,
            key=f"{protected_prefix}/{name}.json",
            kms_key_id=args.kms_key_id,
            payload={
                "schema_version": 4,
                "split": name,
                "variants": split_result[name],
            },
        )
        for name in ("pilot", "confirmation")
    }
    development = {
        "schema_version": 4,
        "split": "development",
        "variants": split_result["development"],
    }
    _write_private_json(args.development_output, development)
    split_receipt = {
        "schema_version": 1,
        "reviewed_pool_sha256": split_result["reviewed_pool_sha256"],
        "sealed_manifest_sha256": split_result["sealed_manifest_sha256"],
        "beacon": split_result["beacon"],
        "development_sha256": _digest(development),
        "protected": protected,
        "retired_sha256": split_result["retired_sha256"],
    }
    _write_private_json(args.split_receipt, split_receipt)
    _print_json(
        {
            "development_sha256": split_receipt["development_sha256"],
            "development_variants": len(development["variants"]),
            "pilot_variants": len(split_result["pilot"]),
            "confirmation_variants": len(split_result["confirmation"]),
        }
    )
    return 0


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return payload


def _write_private_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _require_private_path(path: pathlib.Path) -> None:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("protected corpus and review files must remain outside the repository")


def _digest(payload: Any) -> str:
    from hindsight.evidence_archive import canonical_json_bytes, sha256_hex

    return sha256_hex(canonical_json_bytes(payload))


def _preflight_bedrock(session: Any) -> None:
    control = session.client("bedrock", config=aws_client_config(read_timeout=30))
    logging = control.get_model_invocation_logging_configuration().get("loggingConfig")
    if logging:
        raise RuntimeError("corpus construction requires model invocation logging to be disabled")
    active = {
        str(item.get("inferenceProfileId") or "")
        for item in control.get_paginator("list_inference_profiles")
        .paginate(typeEquals="SYSTEM_DEFINED")
        .search("inferenceProfileSummaries[?status == 'ACTIVE'][]")
        if item
    }
    required = {DRAFTER_MODEL, *ADJUDICATOR_MODELS}
    if not required.issubset(active):
        raise RuntimeError("one or more pinned Bedrock inference profiles are unavailable")


def _print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
