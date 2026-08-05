"""Run explicit stages of the deterministic v5 learning study."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hindsight.v5_corpus import (  # noqa: E402
    development_protocol,
    qualify_development_structure,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("protocol")
    subparsers.add_parser("embedding-protocol")
    structural = subparsers.add_parser("qualify-structure")
    structural.add_argument("--output", type=pathlib.Path)
    embeddings = subparsers.add_parser("qualify-embeddings")
    embeddings.add_argument("--checkpoint", type=pathlib.Path, required=True)
    embeddings.add_argument("--output", type=pathlib.Path, required=True)
    embeddings.add_argument("--diagnostic-output", type=pathlib.Path, required=True)
    args = parser.parse_args()

    if args.command == "protocol":
        _write_json(development_protocol(), output=None)
        return 0
    if args.command == "embedding-protocol":
        from hindsight.v5_qualification import development_qualification_contract

        _write_json(development_qualification_contract(), output=None)
        return 0
    if args.command == "qualify-structure":
        receipt = qualify_development_structure(
            code_sha=_exact_code_sha(),
        )
        _write_json(receipt, output=args.output)
        return 0
    if args.command == "qualify-embeddings":
        receipt = _qualify_embeddings(
            code_sha=_exact_code_sha(),
            checkpoint=args.checkpoint,
            output=args.output,
            diagnostic_output=args.diagnostic_output,
        )
        _write_json(
            {
                "status": receipt["status"],
                "code_sha": receipt["code_sha"],
                "qualification_contract_sha256": receipt["qualification_contract_sha256"],
                "structural_receipt_sha256": receipt["structural_receipt_sha256"],
                "scenario_count": receipt["scenario_count"],
                "receipt_sha256": receipt["receipt_sha256"],
            },
            output=None,
        )
        return 0
    raise AssertionError(f"unsupported v5 command: {args.command}")


def _write_json(value: object, *, output: pathlib.Path | None) -> None:
    rendered = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if output is None:
        sys.stdout.write(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")


def _exact_code_sha() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("v5 qualification requires a clean exact-code checkout")
    code_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", code_sha):
        raise RuntimeError("v5 qualification could not resolve an exact code SHA")
    expected = os.environ.get("GITHUB_SHA")
    if expected and expected != code_sha:
        raise RuntimeError("v5 qualification checkout differs from GITHUB_SHA")
    return code_sha


def _qualify_embeddings(
    *,
    code_sha: str,
    checkpoint: pathlib.Path,
    output: pathlib.Path,
    diagnostic_output: pathlib.Path,
) -> dict[str, object]:
    from hindsight.embeddings import GeminiEmbeddingProvider
    from hindsight.gemini import gemini_pool_from_env
    from hindsight.opaque_tokens import KmsHmacTokenizer
    from hindsight.runtime import runtime_settings
    from hindsight.v5_corpus import (
        EMBEDDING_DIMENSIONS,
        EMBEDDING_MODEL,
        GEMINI_PROVIDER_REPRESENTATION,
    )
    from hindsight.v5_qualification import (
        development_qualification_contract,
        run_development_qualification,
    )

    provider_env = {
        **os.environ,
        "LLM_PROVIDER": "gemini",
        "EMBEDDING_PROVIDER": "gemini",
        "GEMINI_EMBEDDING_MODEL": EMBEDDING_MODEL,
    }
    settings = runtime_settings(
        environ=provider_env,
        use_cache=False,
    )
    pool = gemini_pool_from_env(settings.provider_env)
    provider = GeminiEmbeddingProvider(
        credential_pool=pool,
        model_name=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        representation=GEMINI_PROVIDER_REPRESENTATION,
    )
    runtime_database_url = (os.environ.get("HINDSIGHT_V5_RUNTIME_DATABASE_URL") or "").strip()
    if not runtime_database_url:
        raise RuntimeError(
            "HINDSIGHT_V5_RUNTIME_DATABASE_URL is required for restricted tenant retrieval"
        )
    hmac_key_id = (
        settings.provider_env.get("HINDSIGHT_QUALIFICATION_HMAC_KEY_ID")
        or os.environ.get("HINDSIGHT_QUALIFICATION_HMAC_KEY_ID")
        or ""
    ).strip()
    if not hmac_key_id:
        raise RuntimeError(
            "HINDSIGHT_QUALIFICATION_HMAC_KEY_ID is required for checkpoint attestation"
        )
    checkpoint_attestor = KmsHmacTokenizer(
        key_id=hmac_key_id,
        family_sha256=development_qualification_contract()["qualification_contract_sha256"],
    )
    return run_development_qualification(
        code_sha=code_sha,
        database_url=settings.database_url,
        runtime_database_url=runtime_database_url,
        embedding_provider=provider,
        checkpoint_attestor=checkpoint_attestor,
        checkpoint_path=checkpoint,
        receipt_path=output,
        diagnostic_path=diagnostic_output,
    )


if __name__ == "__main__":
    raise SystemExit(main())
