"""Run pilot, preregistered confirmation, or CI-smoke learning experiments."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import re
import sys
import time
from typing import Any
from uuid import uuid4

from psycopg.rows import dict_row

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.benchmark import (  # noqa: E402
    BenchmarkVariant,
    ALL_SIMULATOR_ACTIONS,
    HELD_OUT_SELECTION_METHOD,
    _deterministic_held_out_order,
    benchmark_report,
    create_experiment,
    finalize_interrupted_experiments,
    preregister_from_completed_pilot,
    run_experiment,
)
from hindsight.consolidation import consolidate_resolved_incident  # noqa: E402
from hindsight.db import connect  # noqa: E402
from hindsight.embedding_index import activate_profile, begin_profile_build  # noqa: E402
from hindsight.embeddings import embedding_profile, embedding_provider_from_env  # noqa: E402
from hindsight.memory import MemoryStore, Provenance  # noqa: E402
from hindsight.reasoning import (  # noqa: E402
    reasoning_provider_from_env,
    retrying_reasoning_provider,
)
from hindsight.runs import create_incident, resolve_incident  # noqa: E402
from hindsight.runtime import runtime_database_url, runtime_settings  # noqa: E402

DEFAULT_CORPUS = pathlib.Path(__file__).resolve().parents[1] / "fixtures/benchmark_variants.json"
CORPUS_SCHEMA_VERSION = 3
PROTOCOL_SCHEMA_VERSION = 3
MIN_PILOT_VARIANTS = 6
MIN_CONFIRMATION_VARIANTS = 12
RETRIEVAL_RANK_REQUIREMENT = 1
BENCHMARK_REASONING_MAX_ATTEMPTS = 4
ARM_NAMES = ("no_lesson", "reference_lesson", "consolidated_lesson")
SIMULATOR_KINDS = (
    "retry_amplification",
    "cache_stampede",
    "connection_leak",
    "hot_partition",
    "poison_message",
    "lock_contention",
)
MAX_PREPARATION_ATTEMPTS = 3
MAX_TARGET_QUERY_OVERLAP = 0.35
MAX_DISTRACTOR_QUERY_OVERLAP = 0.25
_TOKEN_RE = re.compile(r"[a-z0-9]+")


class ScientificBenchmarkFailure(RuntimeError):
    """A claim-bearing benchmark condition failed and must not be retried."""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    pilot = subparsers.add_parser("pilot")
    pilot.add_argument("--repetitions", type=int, default=2)
    pilot.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    pilot.add_argument(
        "--max-distance",
        type=float,
        required=True,
        help="Precommitted cosine-distance cutoff already active in the database profile.",
    )
    preregister = subparsers.add_parser("preregister")
    preregister.add_argument("--pilot-experiment-id", required=True)
    preregister.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    confirmation = subparsers.add_parser("confirmation")
    confirmation.add_argument("--pilot-experiment-id", required=True)
    confirmation.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    smoke = subparsers.add_parser("ci-smoke")
    smoke.add_argument("--corpus", type=pathlib.Path, default=DEFAULT_CORPUS)
    finalize_interrupted = subparsers.add_parser("finalize-interrupted")
    finalize_interrupted.add_argument("--code-sha", required=True)
    finalize_interrupted.add_argument("--reason", required=True)
    args = parser.parse_args()
    if args.command == "finalize-interrupted":
        print(
            json.dumps(
                finalize_interrupted_experiments(
                    code_sha=args.code_sha,
                    reason=args.reason,
                    db_url=runtime_database_url(),
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return
    if args.command == "pilot" and args.repetitions != 2:
        parser.error("claim-bearing pilot requires exactly two repetitions")

    corpus_bytes = args.corpus.read_bytes()
    corpus = json.loads(corpus_bytes)
    _validate_corpus(corpus)
    _require_explicit_live_providers(args.command)
    settings = runtime_settings(use_cache=False)
    reasoning = retrying_reasoning_provider(
        reasoning_provider_from_env(settings.provider_env),
        max_attempts=BENCHMARK_REASONING_MAX_ATTEMPTS,
    )
    embeddings = embedding_provider_from_env(settings.provider_env)
    active_profile = _resolve_active_profile(
        command=args.command,
        db_url=settings.database_url,
        embeddings=embeddings,
        expected_max_distance=getattr(args, "max_distance", None),
    )
    code_sha = _code_sha(args.command)
    claim_family_contract = {
        "protocol_schema_version": PROTOCOL_SCHEMA_VERSION,
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "provider": reasoning.provider_name,
        "model": reasoning.model_name,
        "embedding_profile_id": active_profile.profile_id,
        "embedding_max_distance": active_profile.max_distance,
        "simulator": "multi_mechanism_incident.v1",
        "primary_endpoint": "penalized_action_count",
        "power_target_effect_actions": 1.0,
        "reference_noninferiority_margin_actions": 1.0,
        "repetitions_per_variant": 2,
        "independent_analysis_unit": "simulator_kind",
        "action_vocabulary_sha256": _digest(list(ALL_SIMULATOR_ACTIONS)),
    }
    claim_family_sha256 = _digest(claim_family_contract)
    study_contract = {
        **claim_family_contract,
        "code_sha": code_sha,
    }
    study_key_sha256 = _digest(study_contract)
    manifest_base = {
        "schema_version": PROTOCOL_SCHEMA_VERSION,
        "corpus_schema_version": CORPUS_SCHEMA_VERSION,
        "corpus_sha256": hashlib.sha256(corpus_bytes).hexdigest(),
        "code_sha": code_sha,
        "study_key_sha256": study_key_sha256,
        "claim_family_sha256": claim_family_sha256,
        "provider": reasoning.provider_name,
        "model": reasoning.model_name,
        "embedding_profile_id": active_profile.profile_id,
        "embedding_provider": active_profile.provider,
        "embedding_model": active_profile.model,
        "embedding_capability": active_profile.capability,
        "embedding_max_distance": active_profile.max_distance,
        "simulator": "multi_mechanism_incident.v1",
        "action_budget": 6,
        "arms": list(ARM_NAMES),
        "arm_context_policy": "identical_background_and_hard_distractors",
        "source_evidence_policy": "isolated_namespace",
        "retrieval_rank_requirement": RETRIEVAL_RANK_REQUIREMENT,
        "independent_analysis_unit": "simulator_kind",
        "action_vocabulary": list(ALL_SIMULATOR_ACTIONS),
        "action_vocabulary_sha256": _digest(list(ALL_SIMULATOR_ACTIONS)),
    }
    if args.command == "ci-smoke":
        manifest_base["study_key_sha256"] = None
        manifest_base["claim_family_sha256"] = None
    if args.command == "preregister":
        selected = [row for row in corpus["variants"] if row["split"] == "confirmation"]
        _verify_completed_pilot_configuration(
            pilot_experiment_id=args.pilot_experiment_id,
            manifest_base=manifest_base,
            eligible_variants=selected,
            db_url=settings.database_url,
        )
        preregistration = preregister_from_completed_pilot(
            pilot_experiment_id=args.pilot_experiment_id,
            held_out_variant_ids=[row["variant_id"] for row in selected],
            provider=reasoning.provider_name,
            model=reasoning.model_name,
            embedding_profile_id=active_profile.profile_id,
            additional_contract=_additional_preregistration_contract(
                manifest_base=manifest_base,
                eligible_variants=selected,
            ),
            db_url=settings.database_url,
        )
        print(
            json.dumps(
                {
                    "pilot_experiment_id": args.pilot_experiment_id,
                    "preregistration_sha256": preregistration["sha256"],
                    "eligible_held_out_variant_ids": preregistration[
                        "eligible_held_out_variant_ids"
                    ],
                    "selected_held_out_variant_ids": preregistration["held_out_variant_ids"],
                    "repetitions_per_variant": preregistration["repetitions_per_variant"],
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if args.command in {"pilot", "ci-smoke"}:
        split = "pilot"
        repetitions = args.repetitions if args.command == "pilot" else 1
        kind = "pilot" if args.command == "pilot" else "ci_smoke"
        selected = [row for row in corpus["variants"] if row["split"] == split]
        if args.command == "ci-smoke":
            selected = selected[:1]
        experiment_manifest = _experiment_manifest(
            manifest_base=manifest_base,
            variants=selected,
            repetitions=repetitions,
            evidence_eligible=False,
        )
        if args.command == "pilot":
            experiment_manifest.update(
                _held_out_pool_manifest(
                    [row for row in corpus["variants"] if row["split"] == "confirmation"]
                )
            )
        experiment = create_experiment(
            experiment_kind=kind,
            manifest=experiment_manifest,
            provider=reasoning.provider_name,
            model=reasoning.model_name,
            embedding_profile_id=active_profile.profile_id,
            db_url=settings.database_url,
        )
    else:
        eligible = [row for row in corpus["variants"] if row["split"] == "confirmation"]
        preregistration = _load_durable_preregistration(
            pilot_experiment_id=args.pilot_experiment_id,
            db_url=settings.database_url,
        )
        _verify_preregistration(
            preregistration=preregistration,
            manifest_base=manifest_base,
            eligible=eligible,
        )
        eligible_by_id = {row["variant_id"]: row for row in eligible}
        selected = [
            eligible_by_id[variant_id]
            for variant_id in preregistration["held_out_variant_ids"]
        ]
        repetitions = int(preregistration["repetitions_per_variant"])
        experiment = create_experiment(
            experiment_kind="confirmation",
            manifest={
                **_experiment_manifest(
                    manifest_base=manifest_base,
                    variants=selected,
                    repetitions=repetitions,
                    evidence_eligible=True,
                ),
                "preregistration_sha256": preregistration["sha256"],
            },
            provider=reasoning.provider_name,
            model=reasoning.model_name,
            embedding_profile_id=active_profile.profile_id,
            preregistration=preregistration,
            db_url=settings.database_url,
        )
    if experiment["status"] == "completed":
        print(
            json.dumps(
                benchmark_report(
                    experiment_id=str(experiment["id"]),
                    db_url=settings.database_url,
                ),
                indent=2,
                sort_keys=True,
                default=str,
            )
        )
        return
    variants = [
        _prepare_variant_with_retries(
            row=row,
            experiment_id=str(experiment["id"]),
            db_url=settings.database_url,
            reasoning=reasoning,
            embeddings=embeddings,
            require_rank_one=args.command != "ci-smoke",
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


def _code_sha(command: str) -> str:
    if command == "ci-smoke":
        return "ci-smoke"
    value = (os.environ.get("HINDSIGHT_BENCHMARK_CODE_SHA") or os.environ.get("GITHUB_SHA") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", value):
        raise RuntimeError(
            "live benchmark commands require HINDSIGHT_BENCHMARK_CODE_SHA or GITHUB_SHA"
        )
    return value


def _experiment_manifest(
    *,
    manifest_base: dict[str, Any],
    variants: list[dict[str, Any]],
    repetitions: int,
    evidence_eligible: bool,
) -> dict[str, Any]:
    return {
        **manifest_base,
        "variant_ids": [row["variant_id"] for row in variants],
        "variant_sha256": {row["variant_id"]: _digest(row) for row in variants},
        "variant_query_sha256": {
            row["variant_id"]: hashlib.sha256(
                str(row["recurrence_query"]).encode("utf-8")
            ).hexdigest()
            for row in variants
        },
        "variant_simulator_kind": {
            row["variant_id"]: row["simulator_kind"] for row in variants
        },
        "repetitions": repetitions,
        "evidence_eligible": evidence_eligible,
    }


def _held_out_pool_manifest(variants: list[dict[str, Any]]) -> dict[str, Any]:
    """Freeze the complete confirmation pool before any pilot outcome exists."""

    return {
        "eligible_held_out_variant_ids": [row["variant_id"] for row in variants],
        "eligible_held_out_variant_sha256": {
            row["variant_id"]: _digest(row) for row in variants
        },
        "eligible_held_out_query_sha256": {
            row["variant_id"]: hashlib.sha256(
                str(row["recurrence_query"]).encode("utf-8")
            ).hexdigest()
            for row in variants
        },
        "eligible_held_out_simulator_kind": {
            row["variant_id"]: row["simulator_kind"] for row in variants
        },
    }


def _prepare_variant_with_retries(**kwargs: Any) -> BenchmarkVariant:
    last_error: Exception | None = None
    for attempt in range(1, MAX_PREPARATION_ATTEMPTS + 1):
        try:
            return _prepare_variant(**kwargs)
        except ScientificBenchmarkFailure:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= MAX_PREPARATION_ATTEMPTS:
                raise
            delay = min(30, max(0, int(getattr(exc, "retry_after_seconds", 0) or 0)))
            if delay:
                time.sleep(delay)
    assert last_error is not None
    raise last_error


def _prepare_variant(
    *,
    row: dict[str, Any],
    experiment_id: str,
    db_url: str,
    reasoning: Any,
    embeddings: Any,
    require_rank_one: bool,
) -> BenchmarkVariant:
    base = f"benchmark:{experiment_id}:{row['variant_id']}"
    namespaces = _variant_namespaces(base)
    consolidated_namespace = namespaces["consolidated_lesson"]
    reference_namespace = namespaces["reference_lesson"]
    preparation, lease_owner = _claim_preparation(
        experiment_id=experiment_id,
        row=row,
        db_url=db_url,
    )
    if preparation["status"] == "completed":
        return _prepared_variant(row=row, experiment_id=experiment_id, preparation=preparation)
    if preparation["status"] == "infrastructure_failed":
        raise RuntimeError("maximum benchmark preparation attempts exhausted")
    assert lease_owner is not None
    slug = f"benchmark:{experiment_id}:{row['variant_id']}"
    try:
        if preparation["source_memory_id"] is None:
            with connect(db_url, application_name="hindsight-benchmark-preparation") as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("SELECT * FROM incidents WHERE slug = %s", (slug,))
                    incident = cur.fetchone()
            if incident is None:
                incident = create_incident(
                    slug=slug,
                    title=f"Benchmark source {row['variant_id']}",
                    severity="sev2",
                    summary=row["source_summary"],
                    consolidation_policy="manual",
                    db_url=db_url,
                )
            elif incident["consolidation_policy"] != "manual":
                raise ScientificBenchmarkFailure(
                    "benchmark incident is not isolated from managed consolidation"
                )
            prepared_context = _prepare_shared_arm_context(row=row, embeddings=embeddings)
            source_embedding = embeddings.embed_document(row["source_summary"])
            reference_embedding = embeddings.embed_document(row["reference_lesson"])
            with connect(db_url, application_name="hindsight-benchmark-preparation") as conn:
                with conn.transaction():
                    locked = _lock_preparation(
                        conn,
                        experiment_id=experiment_id,
                        variant_id=row["variant_id"],
                        lease_owner=lease_owner,
                    )
                    if locked["source_memory_id"] is None:
                        store = MemoryStore(conn=conn, embedding_provider=embeddings)
                        _seed_shared_arm_context(
                            store=store,
                            row=row,
                            arm_namespaces={name: namespaces[name] for name in ARM_NAMES},
                            prepared_context=prepared_context,
                        )
                        source = store.write_semantic(
                            namespace=namespaces["source"],
                            content=row["source_summary"],
                            provenance=Provenance(
                                writer="benchmark.source",
                                source_ref=f"benchmark_variant:{row['variant_id']}",
                                justification="Frozen benchmark source episode evidence",
                            ),
                            content_schema="benchmark_source.v1",
                            structured_payload={
                                "variant_id": row["variant_id"],
                                "split": row["split"],
                                "simulator_kind": row["simulator_kind"],
                            },
                            precomputed_embedding=source_embedding,
                        )
                        reference = store.write_semantic(
                            namespace=reference_namespace,
                            content=row["reference_lesson"],
                            provenance=Provenance(
                                writer="benchmark.reference_curator",
                                source_ref=(
                                    f"{row['reference_source']}:{row['variant_id']}"
                                ),
                                justification=(
                                    "Curated reference policy from the external simulator spec"
                                ),
                            ),
                            content_schema="benchmark_reference_lesson.v1",
                            structured_payload={
                                "variant_id": row["variant_id"],
                                "simulator_kind": row["simulator_kind"],
                                "reference_source": row["reference_source"],
                            },
                            precomputed_embedding=reference_embedding,
                        )
                        conn.execute(
                            """
                                INSERT INTO incident_semantic_memories (
                                    incident_id, memory_id, relationship
                                ) VALUES (%s, %s, 'summary')
                                ON CONFLICT (incident_id, memory_id) DO NOTHING
                            """,
                            (incident["id"], source["id"]),
                        )
                        conn.execute(
                            """
                                UPDATE benchmark_variant_preparations
                                SET phase = 'consolidation', incident_id = %s,
                                    source_memory_id = %s, reference_memory_id = %s,
                                    updated_at = now()
                                WHERE experiment_id = %s AND variant_id = %s
                                    AND status = 'leased' AND lease_owner = %s
                            """,
                            (
                                incident["id"],
                                source["id"],
                                reference["id"],
                                experiment_id,
                                row["variant_id"],
                                lease_owner,
                            ),
                        )
            preparation = _get_preparation(
                experiment_id=experiment_id,
                variant_id=row["variant_id"],
                db_url=db_url,
            )

        resolution = resolve_incident(
            slug=slug,
            root_cause=row["root_cause"],
            action=row["resolution_action"],
            observation=row["resolution_observation"],
            recovered=True,
            actor="benchmark.simulator",
            db_url=db_url,
        )
        if preparation["consolidated_memory_id"] is None:
            consolidated = consolidate_resolved_incident(
                incident_id=resolution["incident"]["id"],
                namespace=consolidated_namespace,
                db_url=db_url,
                reasoning_provider=reasoning,
                embedding_provider=embeddings,
            )
            if not consolidated.memory:
                raise ScientificBenchmarkFailure(
                    f"variant {row['variant_id']} produced no eligible consolidated lesson: "
                    f"{consolidated.reason or 'unknown reason'}"
                )
            with connect(db_url, application_name="hindsight-benchmark-preparation") as conn:
                with conn.transaction():
                    _lock_preparation(
                        conn,
                        experiment_id=experiment_id,
                        variant_id=row["variant_id"],
                        lease_owner=lease_owner,
                    )
                    conn.execute(
                        """
                            UPDATE benchmark_variant_preparations
                            SET phase = 'rank_check', consolidated_memory_id = %s,
                                updated_at = now()
                            WHERE experiment_id = %s AND variant_id = %s
                                AND status = 'leased' AND lease_owner = %s
                        """,
                        (
                            consolidated.memory["id"],
                            experiment_id,
                            row["variant_id"],
                            lease_owner,
                        ),
                    )
            preparation = _get_preparation(
                experiment_id=experiment_id,
                variant_id=row["variant_id"],
                db_url=db_url,
            )

        if require_rank_one:
            _require_lesson_rank_one(
                namespace=reference_namespace,
                recurrence_query=row["recurrence_query"],
                expected_memory_id=str(preparation["reference_memory_id"]),
                variant_id=row["variant_id"],
                arm="reference_lesson",
                db_url=db_url,
                embeddings=embeddings,
            )
            _require_lesson_rank_one(
                namespace=consolidated_namespace,
                recurrence_query=row["recurrence_query"],
                expected_memory_id=str(preparation["consolidated_memory_id"]),
                variant_id=row["variant_id"],
                arm="consolidated_lesson",
                db_url=db_url,
                embeddings=embeddings,
            )
        with connect(db_url, application_name="hindsight-benchmark-preparation") as conn:
            updated = conn.execute(
                """
                    UPDATE benchmark_variant_preparations
                    SET phase = 'complete', status = 'completed', lease_owner = NULL,
                        lease_expires_at = NULL, completed_at = now(), updated_at = now()
                    WHERE experiment_id = %s AND variant_id = %s
                        AND status = 'leased' AND lease_owner = %s
                    RETURNING *
                """,
                (experiment_id, row["variant_id"], lease_owner),
            ).fetchone()
            conn.commit()
        if updated is None:
            raise RuntimeError("benchmark preparation lease was lost before completion")
        return _prepared_variant(
            row=row,
            experiment_id=experiment_id,
            preparation=_get_preparation(
                experiment_id=experiment_id,
                variant_id=row["variant_id"],
                db_url=db_url,
            ),
        )
    except ScientificBenchmarkFailure as exc:
        _record_preparation_failure(
            experiment_id=experiment_id,
            variant_id=row["variant_id"],
            lease_owner=lease_owner,
            exc=exc,
            failure_class="scientific",
            db_url=db_url,
        )
        raise
    except Exception as exc:
        _record_preparation_failure(
            experiment_id=experiment_id,
            variant_id=row["variant_id"],
            lease_owner=lease_owner,
            exc=exc,
            failure_class="infrastructure",
            db_url=db_url,
        )
        raise


def _variant_namespaces(base: str) -> dict[str, str]:
    return {
        "source": f"{base}:source",
        "no_lesson": f"{base}:arm:no-lesson",
        "reference_lesson": f"{base}:arm:reference-lesson",
        "consolidated_lesson": f"{base}:arm:consolidated-lesson",
    }


def _claim_preparation(
    *, experiment_id: str, row: dict[str, Any], db_url: str
) -> tuple[dict[str, Any], str | None]:
    lease_owner = f"benchmark-preparation:{uuid4()}"
    definition_sha256 = _digest(row)
    with connect(db_url, application_name="hindsight-benchmark-preparation") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        INSERT INTO benchmark_variant_preparations (
                            experiment_id, variant_id, definition_sha256
                        ) VALUES (%s, %s, %s)
                        ON CONFLICT (experiment_id, variant_id) DO NOTHING
                    """,
                    (experiment_id, row["variant_id"], definition_sha256),
                )
                cur.execute(
                    """
                        SELECT *, now() AS current_time
                        FROM benchmark_variant_preparations
                        WHERE experiment_id = %s AND variant_id = %s
                        FOR UPDATE
                    """,
                    (experiment_id, row["variant_id"]),
                )
                preparation = cur.fetchone()
                if preparation is None:
                    raise RuntimeError("benchmark preparation could not be created")
                if str(preparation["definition_sha256"]) != definition_sha256:
                    raise ScientificBenchmarkFailure(
                        "benchmark preparation definition differs from the frozen manifest"
                    )
                if preparation["status"] == "completed":
                    return dict(preparation), None
                if preparation["status"] == "scientific_failed":
                    raise ScientificBenchmarkFailure(
                        f"benchmark preparation is terminal: {preparation['status']}"
                    )
                if preparation["status"] == "infrastructure_failed":
                    raise RuntimeError(
                        f"benchmark preparation is terminal: {preparation['status']}"
                    )
                if (
                    preparation["status"] == "leased"
                    and preparation["lease_expires_at"] is not None
                    and preparation["lease_expires_at"] > preparation["current_time"]
                ):
                    raise RuntimeError("benchmark preparation already has an active lease")
                if int(preparation["attempt_count"]) >= MAX_PREPARATION_ATTEMPTS:
                    cur.execute(
                        """
                            UPDATE benchmark_variant_preparations
                            SET status = 'infrastructure_failed',
                                failure_class = 'infrastructure',
                                failure_code = 'RetryLimitExceeded',
                                failure_detail = 'maximum preparation attempts exhausted',
                                lease_owner = NULL, lease_expires_at = NULL,
                                completed_at = now(), updated_at = now()
                            WHERE experiment_id = %s AND variant_id = %s
                            RETURNING *
                        """,
                        (experiment_id, row["variant_id"]),
                    )
                    terminal_preparation = cur.fetchone()
                    cur.execute(
                        """
                            UPDATE benchmark_experiments
                            SET status = 'incomplete', completed_at = now()
                            WHERE id = %s AND status = 'created'
                        """,
                        (experiment_id,),
                    )
                    assert terminal_preparation is not None
                    return dict(terminal_preparation), None
                cur.execute(
                    """
                        UPDATE benchmark_variant_preparations
                        SET status = 'leased', attempt_count = attempt_count + 1,
                            lease_owner = %s,
                            lease_expires_at = now() + INTERVAL '15 minutes',
                            failure_class = NULL, failure_code = NULL,
                            failure_detail = NULL, updated_at = now()
                        WHERE experiment_id = %s AND variant_id = %s
                        RETURNING *
                    """,
                    (lease_owner, experiment_id, row["variant_id"]),
                )
                return dict(cur.fetchone()), lease_owner


def _get_preparation(*, experiment_id: str, variant_id: str, db_url: str) -> dict[str, Any]:
    with connect(db_url, application_name="hindsight-benchmark-preparation") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT * FROM benchmark_variant_preparations
                    WHERE experiment_id = %s AND variant_id = %s
                """,
                (experiment_id, variant_id),
            )
            row = cur.fetchone()
    if row is None:
        raise LookupError(f"benchmark preparation missing: {experiment_id}:{variant_id}")
    return dict(row)


def _lock_preparation(
    conn: Any, *, experiment_id: str, variant_id: str, lease_owner: str
) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
                SELECT * FROM benchmark_variant_preparations
                WHERE experiment_id = %s AND variant_id = %s
                    AND status = 'leased' AND lease_owner = %s
                FOR UPDATE
            """,
            (experiment_id, variant_id, lease_owner),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("benchmark preparation lease is no longer owned")
    return dict(row)


def _record_preparation_failure(
    *,
    experiment_id: str,
    variant_id: str,
    lease_owner: str,
    exc: Exception,
    failure_class: str,
    db_url: str,
) -> None:
    with connect(db_url, application_name="hindsight-benchmark-preparation") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        SELECT attempt_count FROM benchmark_variant_preparations
                        WHERE experiment_id = %s AND variant_id = %s
                            AND status = 'leased' AND lease_owner = %s
                        FOR UPDATE
                    """,
                    (experiment_id, variant_id, lease_owner),
                )
                preparation = cur.fetchone()
                if preparation is None:
                    return
                terminal = failure_class == "scientific" or int(
                    preparation["attempt_count"]
                ) >= MAX_PREPARATION_ATTEMPTS
                status = (
                    "scientific_failed"
                    if failure_class == "scientific"
                    else "infrastructure_failed"
                    if terminal
                    else "retrying"
                )
                cur.execute(
                    """
                        UPDATE benchmark_variant_preparations
                        SET status = %s, failure_class = %s, failure_code = %s,
                            failure_detail = %s, lease_owner = NULL,
                            lease_expires_at = NULL,
                            completed_at = CASE WHEN %s THEN now() ELSE NULL END,
                            updated_at = now()
                        WHERE experiment_id = %s AND variant_id = %s
                            AND status = 'leased' AND lease_owner = %s
                    """,
                    (
                        status,
                        failure_class,
                        type(exc).__name__,
                        str(exc)[:1000],
                        terminal,
                        experiment_id,
                        variant_id,
                        lease_owner,
                    ),
                )
                if terminal:
                    cur.execute(
                        """
                            UPDATE benchmark_experiments
                            SET status = %s, completed_at = now()
                            WHERE id = %s AND status = 'created'
                        """,
                        (
                            "failed" if failure_class == "scientific" else "incomplete",
                            experiment_id,
                        ),
                    )


def _prepared_variant(
    *, row: dict[str, Any], experiment_id: str, preparation: dict[str, Any]
) -> BenchmarkVariant:
    if preparation["status"] != "completed":
        raise RuntimeError("benchmark variant preparation is not complete")
    if not preparation["reference_memory_id"] or not preparation["consolidated_memory_id"]:
        raise RuntimeError("benchmark variant preparation is missing lesson identities")
    namespaces = _variant_namespaces(f"benchmark:{experiment_id}:{row['variant_id']}")
    return BenchmarkVariant(
        variant_id=row["variant_id"],
        simulator_kind=row["simulator_kind"],
        recurrence_query=row["recurrence_query"],
        no_lesson_namespace=namespaces["no_lesson"],
        reference_lesson_namespace=namespaces["reference_lesson"],
        consolidated_lesson_namespace=namespaces["consolidated_lesson"],
        reference_lesson_memory_id=str(preparation["reference_memory_id"]),
        consolidated_lesson_memory_id=str(preparation["consolidated_memory_id"]),
        definition_sha256=_digest(row),
        action_budget=6,
    )


def _seed_shared_arm_context(
    *,
    store: Any,
    row: dict[str, Any],
    arm_namespaces: dict[str, str],
    prepared_context: list[tuple[dict[str, Any], list[float]]],
) -> None:
    """Write byte-identical background and distractors into every tested arm."""

    if set(arm_namespaces) != set(ARM_NAMES):
        raise ValueError("all benchmark arms are required for shared context")
    if [item[0] for item in prepared_context] != row["context_memories"]:
        raise ValueError("prepared context differs from the frozen variant")
    for context, prepared_embedding in prepared_context:
        content = str(context["content"])
        payload = {
            "variant_id": row["variant_id"],
            "context_id": context["context_id"],
            "role": context["role"],
        }
        metadata = {"benchmark": True, **payload}
        provenance = Provenance(
            writer="benchmark.context",
            source_ref=f"benchmark_context:{row['variant_id']}:{context['context_id']}",
            justification="Frozen shared benchmark context present in every arm",
        )
        for arm in ARM_NAMES:
            store.write_semantic(
                namespace=arm_namespaces[arm],
                content=content,
                provenance=provenance,
                metadata=metadata,
                content_schema="benchmark_context.v1",
                structured_payload=payload,
                precomputed_embedding=prepared_embedding,
            )


def _prepare_shared_arm_context(
    *, row: dict[str, Any], embeddings: Any
) -> list[tuple[dict[str, Any], list[float]]]:
    """Embed each shared row once, before opening the arm-write transaction."""

    return [
        (context, embeddings.embed_document(str(context["content"])))
        for context in row["context_memories"]
    ]


def _require_lesson_rank_one(
    *,
    namespace: str,
    recurrence_query: str,
    expected_memory_id: str,
    variant_id: str,
    arm: str,
    db_url: str,
    embeddings: Any,
) -> None:
    with MemoryStore(url=db_url, embedding_provider=embeddings) as store:
        hits = store.recall_semantic(
            namespace=namespace,
            query=recurrence_query,
            limit=RETRIEVAL_RANK_REQUIREMENT,
        )
    _assert_expected_first(
        hits=hits,
        expected_memory_id=expected_memory_id,
        variant_id=variant_id,
        arm=arm,
    )


def _assert_expected_first(
    *, hits: list[dict[str, Any]], expected_memory_id: str, variant_id: str, arm: str
) -> None:
    first_id = str(hits[0]["id"]) if hits else None
    if first_id != expected_memory_id:
        raise ScientificBenchmarkFailure(
            f"variant {variant_id} failed rank-one retrieval for {arm}: "
            f"expected {expected_memory_id}, got {first_id or 'empty'}"
        )


def _require_explicit_live_providers(command: str) -> None:
    if command == "ci-smoke":
        return
    missing = [name for name in ("LLM_PROVIDER", "EMBEDDING_PROVIDER") if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "live benchmark commands require explicit provider selection: " + ", ".join(missing)
        )
    if os.environ["LLM_PROVIDER"].strip().lower() == "deterministic":
        raise RuntimeError("live benchmark commands cannot use deterministic reasoning")
    if os.environ["EMBEDDING_PROVIDER"].strip().lower() == "deterministic":
        raise RuntimeError("live benchmark commands require a semantic embedding provider")


def _resolve_active_profile(
    *, command: str, db_url: str, embeddings: Any, expected_max_distance: float | None
) -> Any:
    try:
        with MemoryStore(url=db_url, embedding_provider=embeddings) as store:
            active_profile = store.active_embedding_profile()
    except RuntimeError as exc:
        if command != "ci-smoke" or "no active embedding profile" not in str(exc):
            raise RuntimeError(
                "a live benchmark requires a prebuilt active semantic profile; run the "
                "side-by-side embedding build with a frozen --max-distance first"
            ) from exc
        building = begin_profile_build(provider=embeddings, db_url=db_url)
        activate_profile(profile_id=str(building["id"]), db_url=db_url)
        with MemoryStore(url=db_url, embedding_provider=embeddings) as store:
            active_profile = store.active_embedding_profile()

    configured = embedding_profile(embeddings, max_distance=active_profile.max_distance)
    if configured.profile_id != active_profile.profile_id:
        raise RuntimeError("configured embedding provider does not match the active profile")
    if command != "ci-smoke":
        if embeddings.capability != "semantic" or active_profile.capability != "semantic":
            raise RuntimeError("live benchmark commands require an active semantic profile")
        if active_profile.max_distance is None:
            raise RuntimeError("live benchmark profile must have a precommitted max_distance")
        if not 0 < float(active_profile.max_distance) <= 2:
            raise RuntimeError("active profile max_distance must be within (0, 2]")
        if expected_max_distance is not None and not math.isclose(
            float(active_profile.max_distance),
            float(expected_max_distance),
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise RuntimeError(
                "pilot --max-distance does not match the database-active embedding profile"
            )
    return active_profile


def _verify_completed_pilot_configuration(
    *,
    pilot_experiment_id: str,
    manifest_base: dict[str, Any],
    eligible_variants: list[dict[str, Any]],
    db_url: str,
) -> None:
    with connect(db_url, application_name="hindsight-benchmark-preregistration") as conn:
        row = conn.execute(
            """
                SELECT experiment_kind, status, manifest, provider, model, embedding_profile_id
                FROM benchmark_experiments WHERE id = %s
            """,
            (pilot_experiment_id,),
        ).fetchone()
    if row is None or row[0] != "pilot" or row[1] != "completed":
        raise ValueError("preregistration requires a completed pilot")
    pilot_manifest = dict(row[2])
    expected = {
        "provider": manifest_base["provider"],
        "model": manifest_base["model"],
        "embedding_profile_id": manifest_base["embedding_profile_id"],
    }
    actual = {"provider": row[3], "model": row[4], "embedding_profile_id": str(row[5])}
    if actual != expected:
        raise ValueError("pilot provider, model, or embedding profile differs from preregistration")
    for field in (
        "corpus_schema_version",
        "corpus_sha256",
        "embedding_max_distance",
        "retrieval_rank_requirement",
        "arm_context_policy",
        "source_evidence_policy",
        "schema_version",
        "study_key_sha256",
        "claim_family_sha256",
        "code_sha",
        "simulator",
        "independent_analysis_unit",
        "action_vocabulary",
        "action_vocabulary_sha256",
    ):
        if pilot_manifest.get(field) != manifest_base[field]:
            raise ValueError(f"pilot configuration drift: {field}")
    expected_pool = _held_out_pool_manifest(eligible_variants)
    for field, expected_value in expected_pool.items():
        if pilot_manifest.get(field) != expected_value:
            raise ValueError(f"pilot held-out pool drift: {field}")


def _additional_preregistration_contract(
    *,
    manifest_base: dict[str, Any],
    eligible_variants: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "corpus_schema_version": manifest_base["corpus_schema_version"],
        "corpus_sha256": manifest_base["corpus_sha256"],
        "held_out_variant_sha256": {row["variant_id"]: _digest(row) for row in eligible_variants},
        "embedding_max_distance": manifest_base["embedding_max_distance"],
        "retrieval_rank_requirement": RETRIEVAL_RANK_REQUIREMENT,
        "arm_context_policy": manifest_base["arm_context_policy"],
        "source_evidence_policy": manifest_base["source_evidence_policy"],
        "study_key_sha256": manifest_base["study_key_sha256"],
        "claim_family_sha256": manifest_base["claim_family_sha256"],
        "code_sha": manifest_base["code_sha"],
        "variant_query_sha256": {
            row["variant_id"]: hashlib.sha256(
                str(row["recurrence_query"]).encode("utf-8")
            ).hexdigest()
            for row in eligible_variants
        },
        "variant_simulator_kind": {
            row["variant_id"]: row["simulator_kind"] for row in eligible_variants
        },
        "action_vocabulary": list(ALL_SIMULATOR_ACTIONS),
        "action_vocabulary_sha256": _digest(list(ALL_SIMULATOR_ACTIONS)),
    }


def _load_durable_preregistration(*, pilot_experiment_id: str, db_url: str) -> dict[str, Any]:
    with connect(db_url, application_name="hindsight-benchmark-confirmation") as conn:
        row = conn.execute(
            """
                SELECT preregistration, preregistration_sha256, confirmation_experiment_id
                FROM benchmark_confirmation_preregistrations
                WHERE pilot_experiment_id = %s
            """,
            (pilot_experiment_id,),
        ).fetchone()
    if row is None:
        raise ValueError("confirmation preregistration has not been prepared durably")
    preregistration = dict(row[0])
    if str(row[1]) != str(preregistration.get("sha256")):
        raise ValueError("durable preregistration digest column mismatch")
    return preregistration


def _verify_preregistration(
    *,
    preregistration: dict[str, Any],
    manifest_base: dict[str, Any],
    eligible: list[dict[str, Any]],
) -> None:
    claimed = preregistration.get("sha256")
    unsigned = {key: value for key, value in preregistration.items() if key != "sha256"}
    if claimed != _digest(unsigned):
        raise ValueError("preregistration digest mismatch")
    eligible_ids = sorted(row["variant_id"] for row in eligible)
    if sorted(preregistration.get("eligible_held_out_variant_ids") or []) != eligible_ids:
        raise ValueError("preregistration eligible variants differ from the frozen corpus")
    selected_ids = list(preregistration.get("held_out_variant_ids") or [])
    if not selected_ids or not set(selected_ids).issubset(set(eligible_ids)):
        raise ValueError("preregistration selected variants are not a frozen eligible subset")
    expected_hashes = {row["variant_id"]: _digest(row) for row in eligible}
    additional = preregistration.get("additional_contract")
    if not isinstance(additional, dict):
        raise ValueError("preregistration is missing its additional benchmark contract")
    expected_selection = _deterministic_held_out_order(
        variant_ids=eligible_ids,
        contract=additional,
    )
    if (
        preregistration.get("held_out_selection_method") != HELD_OUT_SELECTION_METHOD
        or selected_ids != expected_selection
    ):
        raise ValueError("preregistration held-out selection differs from the frozen algorithm")
    checks = {
        "corpus_schema_version": manifest_base["corpus_schema_version"],
        "corpus_sha256": manifest_base["corpus_sha256"],
        "held_out_variant_sha256": expected_hashes,
        "embedding_max_distance": manifest_base["embedding_max_distance"],
        "retrieval_rank_requirement": RETRIEVAL_RANK_REQUIREMENT,
        "arm_context_policy": manifest_base["arm_context_policy"],
        "source_evidence_policy": manifest_base["source_evidence_policy"],
        "study_key_sha256": manifest_base["study_key_sha256"],
        "claim_family_sha256": manifest_base["claim_family_sha256"],
        "code_sha": manifest_base["code_sha"],
        "variant_query_sha256": {
            row["variant_id"]: hashlib.sha256(
                str(row["recurrence_query"]).encode("utf-8")
            ).hexdigest()
            for row in eligible
        },
        "variant_simulator_kind": {
            row["variant_id"]: row["simulator_kind"] for row in eligible
        },
        "action_vocabulary": list(ALL_SIMULATOR_ACTIONS),
        "action_vocabulary_sha256": _digest(list(ALL_SIMULATOR_ACTIONS)),
    }
    for field, expected in checks.items():
        if additional.get(field) != expected:
            raise ValueError(f"confirmation configuration drift: {field}")
    for field, expected in (
        ("provider", manifest_base["provider"]),
        ("model", manifest_base["model"]),
        ("embedding_profile_id", manifest_base["embedding_profile_id"]),
    ):
        if preregistration.get(field) != expected:
            raise ValueError(f"confirmation configuration drift: {field}")


def _validate_corpus(corpus: dict[str, Any]) -> None:
    if corpus.get("schema_version") != CORPUS_SCHEMA_VERSION:
        raise ValueError("unsupported benchmark corpus schema")
    variants = corpus.get("variants")
    if not isinstance(variants, list):
        raise ValueError("benchmark variants must be a list")
    split_counts = {
        split: sum(1 for row in variants if row.get("split") == split)
        for split in ("pilot", "confirmation")
    }
    if split_counts["pilot"] < MIN_PILOT_VARIANTS:
        raise ValueError(f"benchmark corpus requires at least {MIN_PILOT_VARIANTS} pilot variants")
    if split_counts["confirmation"] < MIN_CONFIRMATION_VARIANTS:
        raise ValueError(
            f"benchmark corpus requires at least {MIN_CONFIRMATION_VARIANTS} confirmation variants"
        )
    if split_counts != {"pilot": 6, "confirmation": 12}:
        raise ValueError("benchmark corpus requires exactly six pilot and twelve confirmation variants")
    variant_ids = [str(row.get("variant_id") or "") for row in variants]
    if any(not value for value in variant_ids) or len(variant_ids) != len(set(variant_ids)):
        raise ValueError("benchmark variant IDs must be nonempty and unique")
    for split, expected_per_family in (("pilot", 1), ("confirmation", 2)):
        for simulator_kind in SIMULATOR_KINDS:
            count = sum(
                row.get("split") == split and row.get("simulator_kind") == simulator_kind
                for row in variants
            )
            if count != expected_per_family:
                raise ValueError(
                    f"benchmark {split} split must contain {expected_per_family} "
                    f"{simulator_kind} variants"
                )
    for row in variants:
        if row.get("simulator_kind") not in SIMULATOR_KINDS:
            raise ValueError(f"variant {row['variant_id']} has an unsupported simulator")
        if row.get("reference_source") != "project-curated-simulator-spec-v1":
            raise ValueError(f"variant {row['variant_id']} has an unsupported reference source")
        contexts = row.get("context_memories")
        if not isinstance(contexts, list):
            raise ValueError(f"variant {row['variant_id']} requires context_memories")
        roles = [context.get("role") for context in contexts]
        if roles.count("background") < 1 or roles.count("hard_distractor") < 2:
            raise ValueError(
                f"variant {row['variant_id']} requires background and two hard distractors"
            )
        query = str(row.get("recurrence_query") or "")
        source_overlap = _lexical_overlap(query, str(row.get("source_summary") or ""))
        lesson_overlap = _lexical_overlap(query, str(row.get("reference_lesson") or ""))
        if max(source_overlap, lesson_overlap) > MAX_TARGET_QUERY_OVERLAP:
            raise ValueError(f"variant {row['variant_id']} recurrence has excessive target overlap")
        distractor_overlaps = [
            _lexical_overlap(query, str(context.get("content") or ""))
            for context in contexts
            if context.get("role") == "hard_distractor"
        ]
        if (
            not distractor_overlaps
            or max(distractor_overlaps) > MAX_DISTRACTOR_QUERY_OVERLAP
        ):
            raise ValueError(
                f"variant {row['variant_id']} distractors repeat too much of the recurrence query"
            )
        context_ids = [str(context.get("context_id") or "") for context in contexts]
        if any(not value for value in context_ids) or len(context_ids) != len(set(context_ids)):
            raise ValueError(f"variant {row['variant_id']} context IDs must be nonempty and unique")


def _lexical_overlap(left: str, right: str) -> float:
    left_tokens = set(_TOKEN_RE.findall(left.lower()))
    right_tokens = set(_TOKEN_RE.findall(right.lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    main()
