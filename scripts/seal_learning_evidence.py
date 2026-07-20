"""Seal canonical JSON reports in the immutable learning-evidence archive."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.evidence_archive import EvidenceArchive  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--object", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--dependency", action="append", default=[], metavar="NAME=SHA256")
    parser.add_argument("--receipt", type=pathlib.Path)
    args = parser.parse_args()

    objects = _load_named_json(args.object)
    dependencies = _named_values(args.dependency)
    archive = EvidenceArchive(
        bucket=args.bucket,
        client=boto3.client("s3", config=aws_client_config()),
    )
    receipt = archive.seal_bundle(
        evidence_id=args.evidence_id,
        objects=objects,
        dependencies=dependencies,
    )
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.receipt is None:
        sys.stdout.write(rendered)
    else:
        args.receipt.write_text(rendered, encoding="utf-8")
    return 0


def _load_named_json(values: list[str]) -> dict[str, object]:
    paths = _named_values(values)
    if not paths:
        raise ValueError("at least one --object is required")
    return {
        name: json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        for name, path in paths.items()
    }


def _named_values(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, item = value.partition("=")
        if not separator or not name or not item or name in result:
            raise ValueError(f"invalid or duplicate named value: {value}")
        result[name] = item
    return result


if __name__ == "__main__":
    raise SystemExit(main())
