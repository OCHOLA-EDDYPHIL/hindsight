"""Generate the v4 development direct/index representation matrix."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from dataclasses import asdict
from typing import Any
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.embedding_index import activate_profile, begin_profile_build  # noqa: E402
from hindsight.embeddings import (  # noqa: E402
    GEMINI_REPRESENTATIONS,
    GeminiEmbeddingProvider,
    embedding_profile,
)
from hindsight.gemini import gemini_pool_from_env  # noqa: E402
from hindsight.memory import MemoryStore, Provenance  # noqa: E402
from hindsight.rank_diagnostics import indexed_candidates, ranked_candidates  # noqa: E402
from hindsight.representation_selection import (  # noqa: E402
    MAX_DISTANCE,
    build_representation_matrix,
)
from hindsight.tenant import tenant_scope  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
RESEARCH_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", type=pathlib.Path, required=True)
    parser.add_argument(
        "--database-url-template",
        required=True,
        help="Migrated disposable URL containing one {representation} placeholder.",
    )
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    if "{representation}" not in args.database_url_template:
        parser.error("--database-url-template must contain {representation}")
    _require_private_path(args.development)
    _require_private_path(args.output)
    package = _load_json(args.development)
    pool = gemini_pool_from_env(os.environ)
    evaluations: dict[str, list[dict[str, Any]]] = {}
    profiles: dict[str, dict[str, Any]] = {}
    with tenant_scope(RESEARCH_TENANT_ID):
        for representation in GEMINI_REPRESENTATIONS:
            provider = GeminiEmbeddingProvider(
                credential_pool=pool,
                representation=representation,
            )
            db_url = args.database_url_template.format(representation=representation)
            _require_disposable_database(db_url, representation=representation)
            evaluations[representation] = _evaluate_representation(
                package=package,
                representation=representation,
                provider=provider,
                db_url=db_url,
            )
            profile = asdict(embedding_profile(provider, max_distance=MAX_DISTANCE))
            profiles[representation] = {**profile, "representation": representation}
    matrix = build_representation_matrix(
        development_package=package,
        evaluations=evaluations,
        embedding_profiles=profiles,
    )
    _write_private_json(args.output, matrix)
    return 0


def _evaluate_representation(
    *,
    package: dict[str, Any],
    representation: str,
    provider: Any,
    db_url: str,
) -> list[dict[str, Any]]:
    variants = package.get("variants")
    if package.get("split") != "development" or not isinstance(variants, list):
        raise ValueError("representation matrix may use only the development package")
    profile = begin_profile_build(provider=provider, max_distance=MAX_DISTANCE, db_url=db_url)
    activate_profile(profile_id=str(profile["id"]), db_url=db_url)
    results = []
    for item in variants:
        item_token = str(item["variant_id"])
        target_token = _candidate_token(item_token, "target")
        candidates = [
            {
                "token": target_token,
                "role": "target",
                "content": str(item["reference_lesson"]),
            },
            *[
                {
                    "token": _candidate_token(item_token, str(row["context_id"])),
                    "role": str(row["role"]),
                    "content": str(row["content"]),
                }
                for row in item["context_memories"]
            ],
        ]
        query_embedding = provider.embed_query(str(item["recurrence_query"]))
        embedded = [
            {**candidate, "embedding": provider.embed_document(candidate["content"])}
            for candidate in candidates
        ]
        direct = ranked_candidates(
            query_embedding=query_embedding,
            candidates=embedded,
            target_token=target_token,
            max_distance=MAX_DISTANCE,
        )
        namespace = f"v4-representation:{representation}:{item_token}"
        identity_by_memory_id = {}
        with MemoryStore(url=db_url, embedding_provider=provider) as store:
            for candidate in embedded:
                memory = store.write_semantic(
                    namespace=namespace,
                    content=candidate["content"],
                    provenance=Provenance(
                        writer="v4.representation_selection",
                        source_ref=f"v4_development:{item_token}:{candidate['token']}",
                        justification="Development-only neutral representation selection",
                    ),
                    content_schema="v4_representation_candidate.v1",
                    structured_payload={"candidate_token": candidate["token"]},
                    precomputed_embedding=candidate["embedding"],
                )
                identity_by_memory_id[str(memory["id"])] = (
                    candidate["token"],
                    candidate["role"],
                )
            hits = store.search_semantic_vector(
                namespace=namespace,
                query_vector=query_embedding,
                profile_id=str(profile["id"]),
                limit=len(candidates),
            )
        indexed = indexed_candidates(
            hits=hits,
            identity_by_memory_id=identity_by_memory_id,
            target_token=target_token,
            max_distance=MAX_DISTANCE,
        )
        results.append(
            {
                "item_token": item_token,
                "target_token": target_token,
                "direct": _sanitized_rankings(direct["rankings"]),
                "indexed": _sanitized_rankings(indexed["rankings"]),
            }
        )
    return results


def _candidate_token(item_token: str, identity: str) -> str:
    from hindsight.evidence_archive import sha256_hex

    return sha256_hex(f"v4-representation-v1\x1f{item_token}\x1f{identity}".encode())


def _sanitized_rankings(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "candidate_token": str(row["candidate_token"]),
            "distance": float(row["distance"]),
        }
        for row in rows
    ]


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("development package must contain one JSON object")
    return payload


def _write_private_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _require_private_path(path: pathlib.Path) -> None:
    resolved = path.expanduser().resolve()
    if resolved == ROOT or ROOT in resolved.parents:
        raise ValueError("v4 development research files must remain outside the repository")


def _require_disposable_database(db_url: str, *, representation: str) -> None:
    database = unquote(urlparse(db_url).path.lstrip("/")).split("/", 1)[0]
    expected = f"hindsight_representation_{representation}"
    if database != expected or not re.fullmatch(r"[a-z0-9_]+", database):
        raise RuntimeError(f"{representation} matrix requires disposable database {expected}")


if __name__ == "__main__":
    raise SystemExit(main())
