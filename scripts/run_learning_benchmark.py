"""Run pilot, preregistered confirmation, or CI-smoke learning experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.benchmark import (  # noqa: E402
    BenchmarkVariant,
    create_experiment,
    preregister_from_completed_pilot,
    run_experiment,
)
from hindsight.consolidation import consolidate_resolved_incident  # noqa: E402
from hindsight.db import connect  # noqa: E402
from hindsight.embedding_index import activate_profile, begin_profile_build  # noqa: E402
from hindsight.embeddings import embedding_provider_from_env  # noqa: E402
from hindsight.memory import MemoryStore, Provenance  # noqa: E402
from hindsight.reasoning import reasoning_provider_from_env  # noqa: E402
from hindsight.runs import create_incident, resolve_incident  # noqa: E402
from hindsight.runtime import runtime_settings  # noqa: E402

DEFAULT_CORPUS = pathlib.Path(__file__).resolve().parents[1] / "fixtures/benchmark_variants.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    pilot = subparsers.add_parser("pilot")
    pilot.add_argument("--repetitions", type=int, default=2)
    pilot.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    confirmation = subparsers.add_parser("confirmation")
    confirmation.add_argument("--pilot-experiment-id", required=True)
    confirmation.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    smoke = subparsers.add_parser("ci-smoke")
    smoke.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()

    corpus_bytes = args.corpus.read_bytes()
    corpus = json.loads(corpus_bytes)
    if corpus.get("schema_version") != 1:
        raise ValueError("unsupported benchmark corpus schema")
    settings = runtime_settings(use_cache=False)
    reasoning = reasoning_provider_from_env(settings.provider_env)
    embeddings = embedding_provider_from_env(settings.provider_env)
    try:
        with MemoryStore(url=settings.database_url, embedding_provider=embeddings) as store:
            active_profile = store.active_embedding_profile()
    except RuntimeError as exc:
        if "no active embedding profile" not in str(exc):
            raise
        building = begin_profile_build(provider=embeddings, db_url=settings.database_url)
        activate_profile(profile_id=str(building["id"]), db_url=settings.database_url)
        with MemoryStore(url=settings.database_url, embedding_provider=embeddings) as store:
            active_profile = store.active_embedding_profile()
    manifest_base = {
        "schema_version": 1,
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "provider": reasoning.provider_name,
        "model": reasoning.model_name,
        "embedding_profile_id": active_profile.profile_id,
        "simulator": "incident_retry_pressure.v1",
        "action_budget": 6,
        "arms": ["no_lesson", "gold_lesson", "consolidated_lesson"],
    }
    if args.command in {"pilot", "ci-smoke"}:
        split = "pilot"
        repetitions = args.repetitions if args.command == "pilot" else 1
        kind = "pilot" if args.command == "pilot" else "ci_smoke"
        selected = [row for row in corpus["variants"] if row["split"] == split]
        if args.command == "ci-smoke":
            selected = selected[:1]
        experiment = create_experiment(
            experiment_kind=kind,
            manifest={
                **manifest_base,
                "variant_ids": [row["variant_id"] for row in selected],
                "variant_sha256": {
                    row["variant_id"]: _digest(row) for row in selected
                },
                "repetitions": repetitions,
                "evidence_eligible": False,
            },
            provider=reasoning.provider_name,
            model=reasoning.model_name,
            embedding_profile_id=active_profile.profile_id,
            db_url=settings.database_url,
        )
    else:
        selected = [row for row in corpus["variants"] if row["split"] == "confirmation"]
        preregistration = preregister_from_completed_pilot(
            pilot_experiment_id=args.pilot_experiment_id,
            held_out_variant_ids=[row["variant_id"] for row in selected],
            provider=reasoning.provider_name,
            model=reasoning.model_name,
            embedding_profile_id=active_profile.profile_id,
            db_url=settings.database_url,
        )
        repetitions = int(preregistration["repetitions_per_variant"])
        experiment = create_experiment(
            experiment_kind="confirmation",
            manifest={
                **manifest_base,
                "variant_ids": [row["variant_id"] for row in selected],
                "variant_sha256": {
                    row["variant_id"]: _digest(row) for row in selected
                },
                "repetitions": repetitions,
                "preregistration_sha256": preregistration["sha256"],
                "evidence_eligible": True,
            },
            provider=reasoning.provider_name,
            model=reasoning.model_name,
            embedding_profile_id=active_profile.profile_id,
            preregistration=preregistration,
            db_url=settings.database_url,
        )
    variants = [
        _prepare_variant(
            row=row,
            experiment_id=str(experiment["id"]),
            db_url=settings.database_url,
            reasoning=reasoning,
            embeddings=embeddings,
        )
        for row in selected
    ]
    report = run_experiment(
        experiment_id=str(experiment["id"]),
        variants=variants,
        repetitions=repetitions,
        reasoning_provider=reasoning,
        embedding_provider=embeddings,
        db_url=settings.database_url,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


def _prepare_variant(
    *,
    row: dict[str, Any],
    experiment_id: str,
    db_url: str,
    reasoning: Any,
    embeddings: Any,
) -> BenchmarkVariant:
    base = f"benchmark:{experiment_id}:{row['variant_id']}"
    consolidated_namespace = f"{base}:consolidated"
    gold_namespace = f"{base}:gold"
    slug = f"benchmark:{uuid4()}"
    incident = create_incident(
        slug=slug,
        title=f"Benchmark source {row['variant_id']}",
        severity="sev2",
        summary=row["source_summary"],
        db_url=db_url,
    )
    with MemoryStore(url=db_url, embedding_provider=embeddings) as store:
        source = store.remember(
            memory_kind="semantic",
            namespace=consolidated_namespace,
            content=row["source_summary"],
            provenance=Provenance(
                writer="benchmark.source",
                source_ref=f"benchmark_variant:{row['variant_id']}",
                justification="Frozen benchmark source episode evidence",
            ),
            content_schema="benchmark_source.v1",
            structured_payload={"variant_id": row["variant_id"], "split": row["split"]},
        )
        store.remember(
            memory_kind="semantic",
            namespace=gold_namespace,
            content=row["gold_lesson"],
            provenance=Provenance(
                writer="benchmark.gold_reviewer",
                source_ref=f"gold_review:{row['gold_verified_by']}:{row['variant_id']}",
                justification="Independently reviewed gold procedural lesson",
            ),
            content_schema="benchmark_gold_lesson.v1",
            structured_payload={
                "variant_id": row["variant_id"],
                "verified_by": row["gold_verified_by"],
                "independent_of_consolidator": True,
            },
        )
    with connect(db_url) as conn:
        conn.execute(
            """
                INSERT INTO incident_semantic_memories (incident_id, memory_id, relationship)
                VALUES (%s, %s, 'summary')
            """,
            (incident["id"], source["id"]),
        )
        conn.commit()
    resolution = resolve_incident(
        slug=slug,
        root_cause=row["root_cause"],
        action=row["resolution_action"],
        observation=row["resolution_observation"],
        recovered=True,
        actor="benchmark.simulator",
        db_url=db_url,
    )
    consolidated = consolidate_resolved_incident(
        incident_id=resolution["incident"]["id"],
        namespace=consolidated_namespace,
        db_url=db_url,
        reasoning_provider=reasoning,
        embedding_provider=embeddings,
    )
    if not consolidated.memory:
        raise RuntimeError(f"variant {row['variant_id']} produced no consolidated lesson")
    return BenchmarkVariant(
        variant_id=row["variant_id"],
        recurrence_query=row["recurrence_query"],
        no_lesson_namespace=f"{base}:none",
        gold_lesson_namespace=gold_namespace,
        consolidated_lesson_namespace=consolidated_namespace,
        consolidated_lesson_memory_id=str(consolidated.memory["id"]),
        definition_sha256=_digest(row),
        action_budget=6,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    main()
