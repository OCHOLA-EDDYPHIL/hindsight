"""Evaluate, rehearse, and authorize v5 governance without provider calls."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hindsight.opaque_tokens import KmsHmacTokenizer  # noqa: E402
from hindsight.v5_governance import (  # noqa: E402
    V1_QUALIFICATION_CONTRACT_SHA256,
    authorize_protected_learning,
    evaluate_governance_v2,
    governance_v2_policy,
    governance_v2_policy_artifact,
    run_cache_only_rehearsals,
    verify_governance_v2,
    verify_protected_learning_authorization,
    write_private_json,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    policy = subparsers.add_parser("policy")
    policy.add_argument("--subject-sha", required=True)
    policy.add_argument("--output", type=pathlib.Path)

    evaluate = subparsers.add_parser("evaluate")
    _add_evidence_arguments(evaluate)
    evaluate.add_argument("--output", type=pathlib.Path, required=True)

    verify = subparsers.add_parser("verify")
    _add_evidence_arguments(verify)
    verify.add_argument("--authorization", type=pathlib.Path, required=True)

    rehearse = subparsers.add_parser("rehearse")
    _add_evidence_arguments(rehearse)
    rehearse.add_argument("--authorization", type=pathlib.Path, required=True)
    rehearse.add_argument("--checkpoint", type=pathlib.Path, required=True)
    rehearse.add_argument("--execution-manifest", type=pathlib.Path, required=True)
    rehearse.add_argument("--output", type=pathlib.Path, required=True)

    authorize = subparsers.add_parser("authorize-protected")
    _add_evidence_arguments(authorize)
    authorize.add_argument("--authorization", type=pathlib.Path, required=True)
    authorize.add_argument("--rehearsal-result", type=pathlib.Path, required=True)
    authorize.add_argument("--output", type=pathlib.Path, required=True)

    verify_protected = subparsers.add_parser("verify-protected")
    verify_protected.add_argument("--authorization", type=pathlib.Path, required=True)

    args = parser.parse_args()
    if args.command == "policy":
        value = governance_v2_policy_artifact(
            tested_subject_sha=args.subject_sha,
            policy_evaluator_sha=_exact_code_sha(),
        )
        if args.output is not None:
            write_private_json(args.output, value)
        _print_json(value)
        return 0

    key_id = _hmac_key_id()
    signer = KmsHmacTokenizer(
        key_id=key_id,
        family_sha256=governance_v2_policy()["policy_sha256"],
    )
    if args.command == "verify-protected":
        verified = verify_protected_learning_authorization(
            protected_authorization=_load_json(args.authorization),
            signer=signer,
        )
        _print_json(_summary(verified))
        return 0

    evaluator_sha = _exact_code_sha()
    diagnostic, diagnostic_file_sha256 = _load_diagnostic(
        args.diagnostic,
        expected_file_sha256=args.expected_diagnostic_file_sha256,
    )
    common = {
        "diagnostic": diagnostic,
        "expected_diagnostic_sha256": args.expected_diagnostic_sha256,
        "diagnostic_file_sha256": diagnostic_file_sha256,
        "tested_subject_sha": args.subject_sha,
        "policy_evaluator_sha": evaluator_sha,
        "signer": signer,
    }
    if args.command == "evaluate":
        value = evaluate_governance_v2(**common)
        write_private_json(args.output, value)
    elif args.command == "verify":
        value = verify_governance_v2(
            authorization=_load_json(args.authorization),
            **common,
        )
    elif args.command == "rehearse":
        checkpoint_attestor = KmsHmacTokenizer(
            key_id=key_id,
            family_sha256=V1_QUALIFICATION_CONTRACT_SHA256,
        )
        database_url = (os.environ.get("DATABASE_URL") or "").strip()
        runtime_database_url = (os.environ.get("HINDSIGHT_V5_RUNTIME_DATABASE_URL") or "").strip()
        if not database_url or not runtime_database_url:
            raise RuntimeError(
                "cache-only rehearsals require deploy and restricted runtime database URLs"
            )
        value = run_cache_only_rehearsals(
            authorization=_load_json(args.authorization),
            checkpoint_attestor=checkpoint_attestor,
            checkpoint_path=args.checkpoint,
            execution_manifest=_load_json(args.execution_manifest),
            database_url=database_url,
            runtime_database_url=runtime_database_url,
            progress_callback=_progress,
            **common,
        )
        write_private_json(args.output, value)
    elif args.command == "authorize-protected":
        value = authorize_protected_learning(
            authorization=_load_json(args.authorization),
            rehearsal_result=_load_json(args.rehearsal_result),
            **common,
        )
        write_private_json(args.output, value)
    else:
        raise AssertionError(f"unsupported v5 governance command: {args.command}")
    _print_json(_summary(value))
    return 0


def _add_evidence_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--diagnostic", type=pathlib.Path, required=True)
    parser.add_argument("--expected-diagnostic-sha256", required=True)
    parser.add_argument("--expected-diagnostic-file-sha256", required=True)
    parser.add_argument("--subject-sha", required=True)


def _exact_code_sha() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("v5 governance evaluation requires a clean exact-code checkout")
    code_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
        raise RuntimeError("v5 governance evaluation could not resolve an exact code SHA")
    expected = os.environ.get("GITHUB_SHA")
    if expected and expected != code_sha:
        raise RuntimeError("v5 governance checkout differs from GITHUB_SHA")
    return code_sha


def _hmac_key_id() -> str:
    value = (os.environ.get("HINDSIGHT_QUALIFICATION_HMAC_KEY_ID") or "").strip()
    if not value:
        raise RuntimeError("HINDSIGHT_QUALIFICATION_HMAC_KEY_ID is required")
    return value


def _load_diagnostic(
    path: pathlib.Path,
    *,
    expected_file_sha256: str,
) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    file_sha256 = hashlib.sha256(raw).hexdigest()
    if file_sha256 != expected_file_sha256:
        raise ValueError("diagnostic file identity differs")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("diagnostic must contain one JSON object")
    return value, file_sha256


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return value


def _summary(value: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "status",
        "tested_subject_sha",
        "policy_evaluator_sha",
        "policy_sha256",
        "scenario_count",
        "semantic_rank_one_count",
        "semantic_rank_one_accuracy_display",
        "maximum_distance_delta",
        "artifact_sha256",
    )
    return {field: value[field] for field in fields if field in value}


def _progress(stage: str, current: int, total: int) -> None:
    if current == total or current % 100 == 0 or (total == 60 and current % 10 == 0):
        sys.stderr.write(f"v5 governance {stage}: {current}/{total}\n")
        sys.stderr.flush()


def _print_json(value: object) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
