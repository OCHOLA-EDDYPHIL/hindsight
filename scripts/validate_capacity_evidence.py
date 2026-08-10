"""Fail closed unless capacity evidence matches the bounded qualification protocol."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

TARGETS = {"vectors": 100_000, "tenants": 20, "clients": 20, "backlog_messages": 1_000}


def validate(document: dict[str, Any], *, source_revision: str) -> dict[str, Any]:
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise ValueError("source revision must be a full lowercase Git SHA")
    qualification = document.get("index_qualification") or {}
    if qualification.get("qualified") is not True:
        raise ValueError("capacity evidence requires a qualified populated vector index")
    if re.fullmatch(r"[0-9a-f]{64}", str(qualification.get("artifact_sha256") or "")) is None:
        raise ValueError("capacity evidence requires a full SHA-256 qualification artifact digest")
    if qualification.get("main_sha") != source_revision:
        raise ValueError("index qualification must belong to the exact tested main revision")
    if document.get("source_revision") != source_revision:
        raise ValueError("capacity evidence must belong to the exact tested main revision")
    if document.get("targets") != TARGETS:
        raise ValueError("capacity evidence does not match the bounded target shape")
    if not document.get("method") or not document.get("environment"):
        raise ValueError("capacity evidence requires method and environment")
    measurements = document.get("raw_measurements")
    if not isinstance(measurements, list) or not measurements:
        raise ValueError("capacity evidence requires raw measurements")
    limitations = document.get("limitations")
    if not isinstance(limitations, list) or not limitations:
        raise ValueError("capacity evidence requires explicit limitations")
    return {
        **document,
        "kind": "bounded_capacity_evidence",
        "claim_scope": "benchmark_evidence_not_production_slo",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = validate(json.loads(args.input.read_text()), source_revision=args.source_revision)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
