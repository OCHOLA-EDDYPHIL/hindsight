"""Compare direct and CockroachDB vector ordering without benchmark experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.db import connect  # noqa: E402
from hindsight.embedding_index import activate_profile, begin_profile_build  # noqa: E402
from hindsight.embeddings import (  # noqa: E402
    embedding_profile,
    embedding_provider_from_env,
)
from hindsight.memory import MemoryStore, Provenance  # noqa: E402
from hindsight.rank_diagnostics import (  # noqa: E402
    indexed_candidates,
    opaque_token,
    ranked_candidates,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SYNTHETIC_CORPUS = ROOT / "fixtures/rank_diagnostic_variants.json"
DEFAULT_PILOT_CORPUS = ROOT / "fixtures/benchmark_variants.json"
BENCHMARK_TABLES = (
    "benchmark_experiments",
    "benchmark_trials",
    "benchmark_actions",
    "benchmark_confirmation_preregistrations",
    "benchmark_confirmation_bindings",
    "benchmark_variant_preparations",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("synthetic", "pilot"))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--code-sha", required=True)
    parser.add_argument("--max-distance", type=float, default=0.35)
    parser.add_argument("--corpus", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--require-target-rank-one", action="store_true")
    args = parser.parse_args()

    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    if not re.fullmatch(r"[0-9a-f]{40,64}", args.code_sha):
        parser.error("--code-sha must be a full hexadecimal revision")
    _require_disposable_database(args.database_url)
    corpus_path = args.corpus or (
        DEFAULT_SYNTHETIC_CORPUS if args.mode == "synthetic" else DEFAULT_PILOT_CORPUS
    )
    corpus_bytes = corpus_path.read_bytes()
    variants = _load_variants(mode=args.mode, corpus=json.loads(corpus_bytes))
    provider = embedding_provider_from_env()
    if args.mode == "pilot" and provider.capability != "semantic":
        parser.error("pilot diagnostics require an explicit semantic embedding provider")

    active_profile = _active_or_empty_profile(
        db_url=args.database_url,
        provider=provider,
        max_distance=args.max_distance,
    )
    before = _benchmark_counts(args.database_url)
    corpus_sha256 = hashlib.sha256(corpus_bytes).hexdigest()
    run_token = opaque_token(args.code_sha, corpus_sha256, uuid4().hex)
    reports = [
        _diagnose_variant(
            row=row,
            mode=args.mode,
            corpus_sha256=corpus_sha256,
            code_sha=args.code_sha,
            run_token=run_token,
            db_url=args.database_url,
            provider=provider,
            profile_id=str(active_profile["id"]),
            max_distance=float(active_profile["max_distance"]),
        )
        for row in variants
    ]
    after = _benchmark_counts(args.database_url)
    if before != after:
        raise RuntimeError("rank diagnostics changed benchmark evidence tables")
    if not all(report["index_parity"] for report in reports):
        raise RuntimeError("direct and indexed diagnostic ordering differ")
    if args.require_target_rank_one and not all(
        report["direct"]["target_rank_one"]
        and report["indexed"]["target_rank_one"]
        for report in reports
    ):
        raise RuntimeError("synthetic diagnostic target did not rank first")

    payload = {
        "schema_version": 1,
        "mode": args.mode,
        "corpus_sha256": corpus_sha256,
        "code_sha": args.code_sha,
        "profile": {
            "id": str(active_profile["id"]),
            "provider": str(active_profile["provider"]),
            "model": str(active_profile["model"]),
            "capability": str(active_profile["capability"]),
            "max_distance": float(active_profile["max_distance"]),
        },
        "benchmark_state_unchanged": True,
        "benchmark_row_counts": before,
        "all_targets_rank_one": all(
            report["direct"]["target_rank_one"]
            and report["indexed"]["target_rank_one"]
            for report in reports
        ),
        "variants": reports,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


def _load_variants(*, mode: str, corpus: dict[str, Any]) -> list[dict[str, Any]]:
    rows = corpus.get("variants")
    if not isinstance(rows, list) or not rows:
        raise ValueError("diagnostic corpus must contain variants")
    if mode == "pilot":
        selected = [row for row in rows if row.get("split") == "pilot"]
        return [
            {
                "id": str(row["variant_id"]),
                "query": str(row["recurrence_query"]),
                "target": str(row["reference_lesson"]),
                "candidates": [
                    {
                        "id": str(candidate["context_id"]),
                        "role": str(candidate["role"]),
                        "content": str(candidate["content"]),
                    }
                    for candidate in row["context_memories"]
                ],
            }
            for row in selected
        ]
    selected = []
    for row in rows:
        selected.append(
            {
                "id": str(row["diagnostic_id"]),
                "query": str(row["query"]),
                "target": str(row["target"]),
                "candidates": [
                    {
                        "id": str(candidate["candidate_id"]),
                        "role": str(candidate["role"]),
                        "content": str(candidate["content"]),
                    }
                    for candidate in row["candidates"]
                ],
            }
        )
    return selected


def _diagnose_variant(
    *,
    row: dict[str, Any],
    mode: str,
    corpus_sha256: str,
    code_sha: str,
    run_token: str,
    db_url: str,
    provider: Any,
    profile_id: str,
    max_distance: float,
) -> dict[str, Any]:
    variant_token = opaque_token(corpus_sha256, row["id"])
    target_token = opaque_token(variant_token, "target")
    candidates = [
        {
            "token": target_token,
            "role": "target",
            "content": row["target"],
        },
        *[
            {
                "token": opaque_token(variant_token, candidate["id"]),
                "role": candidate["role"],
                "content": candidate["content"],
            }
            for candidate in row["candidates"]
        ],
    ]
    query_embedding = provider.embed_query(row["query"])
    embedded = [
        {
            **candidate,
            "embedding": provider.embed_document(candidate["content"]),
        }
        for candidate in candidates
    ]
    direct = ranked_candidates(
        query_embedding=query_embedding,
        candidates=embedded,
        target_token=target_token,
        max_distance=max_distance,
    )
    namespace = f"rank-diagnostic:{run_token}:{variant_token}"
    identity_by_memory_id: dict[str, tuple[str, str]] = {}
    with MemoryStore(url=db_url, embedding_provider=provider) as store:
        for candidate in embedded:
            memory = store.write_semantic(
                namespace=namespace,
                content=candidate["content"],
                provenance=Provenance(
                    writer="rank.diagnostic",
                    source_ref=f"rank_diagnostic:{variant_token}:{candidate['token']}",
                    justification="Disposable outcome-free vector ordering diagnostic",
                ),
                content_schema="rank_diagnostic.v1",
                structured_payload={
                    "variant_token": variant_token,
                    "candidate_token": candidate["token"],
                    "candidate_role": candidate["role"],
                    "mode": mode,
                    "code_sha": code_sha,
                },
                precomputed_embedding=candidate["embedding"],
            )
            identity_by_memory_id[str(memory["id"])] = (
                candidate["token"],
                candidate["role"],
            )
        hits = store.search_semantic_vector(
            namespace=namespace,
            query_vector=query_embedding,
            profile_id=profile_id,
            limit=len(candidates),
        )
    indexed = indexed_candidates(
        hits=hits,
        identity_by_memory_id=identity_by_memory_id,
        target_token=target_token,
    )
    return {
        "variant_token": variant_token,
        "candidate_count": len(candidates),
        "direct": direct,
        "indexed": indexed,
        "index_parity": [
            row["candidate_token"] for row in direct["rankings"]
        ]
        == [row["candidate_token"] for row in indexed["rankings"]],
    }


def _active_or_empty_profile(
    *, db_url: str, provider: Any, max_distance: float
) -> dict[str, Any]:
    with MemoryStore(url=db_url, embedding_provider=provider) as store:
        try:
            active = store.active_embedding_profile()
        except RuntimeError as exc:
            if "no active embedding profile" not in str(exc):
                raise
            active = None
    if active is None:
        building = begin_profile_build(
            provider=provider,
            max_distance=max_distance,
            db_url=db_url,
        )
        activate_profile(profile_id=str(building["id"]), db_url=db_url)
        with MemoryStore(url=db_url, embedding_provider=provider) as store:
            active = store.active_embedding_profile()
    configured = embedding_profile(provider, max_distance=active.max_distance)
    if configured.profile_id != active.profile_id:
        raise RuntimeError("configured provider differs from diagnostic database profile")
    if active.max_distance is None or float(active.max_distance) != max_distance:
        raise RuntimeError("diagnostic cutoff differs from the active profile")
    return {
        "id": active.profile_id,
        "provider": active.provider,
        "model": active.model,
        "capability": active.capability,
        "max_distance": active.max_distance,
    }


def _benchmark_counts(db_url: str) -> dict[str, int]:
    with connect(db_url, application_name="hindsight-rank-diagnostic") as conn:
        return {
            table: int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in BENCHMARK_TABLES
        }


def _require_disposable_database(db_url: str) -> None:
    database_name = unquote(urlparse(db_url).path.lstrip("/")).split("/", 1)[0]
    if not re.fullmatch(r"hindsight_diagnostic(?:_[a-z0-9_]+)?", database_name):
        raise RuntimeError(
            "rank diagnostics require a disposable hindsight_diagnostic database"
        )


if __name__ == "__main__":
    main()
