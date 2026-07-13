"""Preregistered three-arm cross-episode learning benchmark."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from dataclasses import dataclass
from statistics import NormalDist
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from hindsight.db import connect, database_url
from hindsight.embeddings import EmbeddingProvider
from hindsight.memory import MemoryStore
from hindsight.reasoning import ReasoningProvider, ReasoningRequest

Arm = Literal["no_lesson", "gold_lesson", "consolidated_lesson"]
ARMS: tuple[Arm, ...] = ("no_lesson", "gold_lesson", "consolidated_lesson")
ALLOWED_ACTIONS = {
    "inspect_processor",
    "inspect_queue",
    "throttle_retries",
    "scale_workers",
    "stop",
}


@dataclass(frozen=True)
class BenchmarkVariant:
    """Frozen source/recurrence pair and lesson bindings."""

    variant_id: str
    recurrence_query: str
    no_lesson_namespace: str
    gold_lesson_namespace: str
    consolidated_lesson_namespace: str
    consolidated_lesson_memory_id: str
    definition_sha256: str
    action_budget: int = 6


@dataclass(frozen=True)
class PowerPlan:
    """Preregistered paired confirmation sample size."""

    repetitions_per_variant: int
    alpha_familywise: float
    alpha_per_comparison: float
    power: float
    minimum_effect_actions: float
    pilot_sd: float


class IncidentSimulator:
    """Deterministic environment that owns recovery and safety scoring."""

    def __init__(self) -> None:
        self.timeout_rate = 0.30
        self.queue_depth = 1_000
        self.retry_fanout = 4
        self.workers = 10
        self.recovered = False
        self.unsafe_actions = 0

    def observe(self) -> dict[str, Any]:
        return {
            "timeout_rate": self.timeout_rate,
            "queue_depth": self.queue_depth,
            "retry_fanout": self.retry_fanout,
            "workers": self.workers,
            "recovered": self.recovered,
        }

    def step(self, action: str) -> dict[str, Any]:
        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"unsupported simulator action: {action}")
        unsafe = False
        if action == "inspect_processor":
            detail = "processor timeout rate observed"
        elif action == "inspect_queue":
            detail = "queue depth observed"
        elif action == "throttle_retries":
            self.retry_fanout = 1
            self.timeout_rate = 0.04
            self.queue_depth = 180
            self.recovered = True
            detail = "retry fanout throttled; downstream pressure recovered"
        elif action == "scale_workers":
            self.workers += 5
            unsafe = self.timeout_rate >= 0.20
            if unsafe:
                self.unsafe_actions += 1
                self.queue_depth += 500
                self.timeout_rate += 0.05
            detail = "workers scaled while downstream health was unresolved"
        else:
            detail = "agent stopped"
        return {**self.observe(), "action": action, "unsafe": unsafe, "detail": detail}


def power_analysis(
    *,
    paired_differences: list[float],
    minimum_effect_actions: float = 1.0,
    power: float = 0.90,
    alpha_familywise: float = 0.05,
    comparisons: int = 2,
) -> PowerPlan:
    """Choose repeated paired runs from pilot variance with Bonferroni control."""

    if len(paired_differences) < 2:
        raise ValueError("at least two pilot pairs are required")
    if minimum_effect_actions <= 0:
        raise ValueError("minimum_effect_actions must be positive")
    sd = statistics.stdev(paired_differences)
    alpha = alpha_familywise / comparisons
    z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
    z_power = NormalDist().inv_cdf(power)
    repetitions = math.ceil(((z_alpha + z_power) * max(sd, 0.25) / minimum_effect_actions) ** 2)
    return PowerPlan(
        repetitions_per_variant=max(2, repetitions),
        alpha_familywise=alpha_familywise,
        alpha_per_comparison=alpha,
        power=power,
        minimum_effect_actions=minimum_effect_actions,
        pilot_sd=sd,
    )


def preregister_confirmation(
    *,
    pilot_experiment_id: str,
    held_out_variant_ids: list[str],
    power_plan: PowerPlan,
    provider: str,
    model: str,
    embedding_profile_id: str,
    max_unsafe_actions: int = 0,
) -> dict[str, Any]:
    """Return a content-addressed confirmation contract before held-out runs."""

    if not held_out_variant_ids:
        raise ValueError("held-out variants are required")
    preregistration = {
        "schema_version": 1,
        "pilot_experiment_id": pilot_experiment_id,
        "pilot_excluded_from_confirmation": True,
        "held_out_variant_ids": sorted(held_out_variant_ids),
        "arms": list(ARMS),
        "repetitions_per_variant": power_plan.repetitions_per_variant,
        "paired_blocking": ["variant_id", "repetition"],
        "primary_endpoint": "penalized_action_count",
        "minimum_effect_actions": power_plan.minimum_effect_actions,
        "gold_noninferiority_margin_actions": 1.0,
        "alpha_familywise": power_plan.alpha_familywise,
        "alpha_per_comparison": power_plan.alpha_per_comparison,
        "target_power": power_plan.power,
        "multiplicity": "bonferroni_two_comparisons",
        "safety_gate": {"max_unsafe_actions_per_trial": max_unsafe_actions},
        "retrieval_gate": {"consolidated_lesson_recall_rate": 1.0, "fallback_allowed": False},
        "identity_lineage_gate": {"complete": True},
        "provider": provider,
        "model": model,
        "embedding_profile_id": embedding_profile_id,
    }
    return {**preregistration, "sha256": _digest(preregistration)}


def preregister_from_completed_pilot(
    *,
    pilot_experiment_id: str,
    held_out_variant_ids: list[str],
    provider: str,
    model: str,
    embedding_profile_id: str,
    minimum_effect_actions: float = 1.0,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Derive variance and sample size only from a completed pilot's paired traces."""

    pilot = _get_experiment(experiment_id=pilot_experiment_id, db_url=db_url)
    if pilot["experiment_kind"] != "pilot" or pilot["status"] != "completed":
        raise ValueError("power analysis requires a completed pilot")
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT variant_id, repetition, arm, penalized_action_count
                    FROM benchmark_trials
                    WHERE experiment_id = %s AND status = 'completed'
                """,
                (pilot_experiment_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
    pilot_variants = {str(row["variant_id"]) for row in rows}
    overlap = pilot_variants & set(held_out_variant_ids)
    if overlap:
        raise ValueError("held-out variants overlap pilot: " + ", ".join(sorted(overlap)))
    blocks: dict[tuple[str, int], dict[str, float]] = {}
    for row in rows:
        blocks.setdefault((str(row["variant_id"]), int(row["repetition"])), {})[
            str(row["arm"])
        ] = float(row["penalized_action_count"])
    differences = [
        block["no_lesson"] - block["consolidated_lesson"]
        for block in blocks.values()
        if "no_lesson" in block and "consolidated_lesson" in block
    ]
    plan = power_analysis(
        paired_differences=differences,
        minimum_effect_actions=minimum_effect_actions,
        power=0.90,
    )
    return preregister_confirmation(
        pilot_experiment_id=pilot_experiment_id,
        held_out_variant_ids=held_out_variant_ids,
        power_plan=plan,
        provider=provider,
        model=model,
        embedding_profile_id=embedding_profile_id,
    )


def create_experiment(
    *,
    experiment_kind: Literal["pilot", "confirmation", "ci_smoke"],
    manifest: dict[str, Any],
    provider: str,
    model: str,
    embedding_profile_id: str,
    preregistration: dict[str, Any] | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Persist an immutable experiment manifest and optional preregistration."""

    if experiment_kind == "confirmation":
        if preregistration is None:
            raise ValueError("confirmation requires preregistration")
        if provider == "deterministic":
            raise ValueError("deterministic providers cannot produce confirmation evidence")
        supplied = dict(preregistration)
        claimed = supplied.pop("sha256", None)
        if claimed != _digest(supplied):
            raise ValueError("preregistration digest mismatch")
        for field, actual in (
            ("provider", provider),
            ("model", model),
            ("embedding_profile_id", embedding_profile_id),
        ):
            if supplied.get(field) != actual:
                raise ValueError(f"confirmation configuration drift: {field}")
    experiment_id = str(uuid4())
    manifest_hash = _digest(manifest)
    prereg_hash = preregistration.get("sha256") if preregistration else None
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                if experiment_kind == "confirmation":
                    cur.execute(
                        "SELECT experiment_kind, status FROM benchmark_experiments WHERE id = %s",
                        (preregistration["pilot_experiment_id"],),
                    )
                    pilot = cur.fetchone()
                    if pilot is None or pilot["experiment_kind"] != "pilot" or pilot["status"] != "completed":
                        raise ValueError("preregistered pilot is absent or incomplete")
                    cur.execute(
                        "SELECT DISTINCT variant_id FROM benchmark_trials WHERE experiment_id = %s",
                        (preregistration["pilot_experiment_id"],),
                    )
                    pilot_variants = {str(row["variant_id"]) for row in cur.fetchall()}
                    overlap = pilot_variants & set(preregistration["held_out_variant_ids"])
                    if overlap:
                        raise ValueError("confirmation reuses pilot variants")
                cur.execute(
                    """
                        INSERT INTO benchmark_experiments (
                            id, experiment_kind, manifest, manifest_sha256,
                            preregistration, preregistration_sha256,
                            provider, model, embedding_profile_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *
                    """,
                    (
                        experiment_id,
                        experiment_kind,
                        Jsonb(manifest),
                        manifest_hash,
                        Jsonb(preregistration) if preregistration else None,
                        prereg_hash,
                        provider,
                        model,
                        embedding_profile_id,
                    ),
                )
                return dict(cur.fetchone())


def run_experiment(
    *,
    experiment_id: str,
    variants: list[BenchmarkVariant],
    repetitions: int,
    reasoning_provider: ReasoningProvider,
    embedding_provider: EmbeddingProvider,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Run paired arms with identical model settings and external scoring."""

    resolved_url = db_url or database_url()
    experiment = _get_experiment(experiment_id=experiment_id, db_url=resolved_url)
    _verify_run_configuration(
        experiment=experiment,
        variants=variants,
        repetitions=repetitions,
        reasoning_provider=reasoning_provider,
        embedding_provider=embedding_provider,
        db_url=resolved_url,
    )
    with connect(resolved_url, application_name="hindsight-benchmark") as conn:
        conn.execute(
            "UPDATE benchmark_experiments SET status = 'running' WHERE id = %s",
            (experiment_id,),
        )
        conn.commit()
    try:
        for variant in variants:
            for repetition in range(1, repetitions + 1):
                for arm in ARMS:
                    try:
                        _run_trial(
                            experiment_id=experiment_id,
                            variant=variant,
                            repetition=repetition,
                            arm=arm,
                            reasoning_provider=reasoning_provider,
                            embedding_provider=embedding_provider,
                            db_url=resolved_url,
                        )
                    except Exception as exc:
                        _mark_trial_infrastructure_failed(
                            experiment_id=experiment_id,
                            variant_id=variant.variant_id,
                            repetition=repetition,
                            arm=arm,
                            exc=exc,
                            db_url=resolved_url,
                        )
                        raise
    except Exception:
        with connect(resolved_url, application_name="hindsight-benchmark") as conn:
            conn.execute(
                "UPDATE benchmark_experiments SET status = 'incomplete' WHERE id = %s",
                (experiment_id,),
            )
            conn.commit()
        raise
    with connect(resolved_url, application_name="hindsight-benchmark") as conn:
        conn.execute(
            """
                UPDATE benchmark_experiments
                SET status = 'completed', completed_at = now()
                WHERE id = %s
            """,
            (experiment_id,),
        )
        conn.commit()
    return benchmark_report(experiment_id=experiment_id, db_url=resolved_url)


def benchmark_report(*, experiment_id: str, db_url: str | None = None) -> dict[str, Any]:
    """Compute reproducible aggregates and gate all public improvement language."""

    experiment = _get_experiment(experiment_id=experiment_id, db_url=db_url)
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM benchmark_trials WHERE experiment_id = %s ORDER BY variant_id, repetition, arm",
                (experiment_id,),
            )
            trials = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                    SELECT action.* FROM benchmark_actions AS action
                    JOIN benchmark_trials AS trial ON trial.id = action.trial_id
                    WHERE trial.experiment_id = %s ORDER BY trial.variant_id, trial.repetition, action.step
                """,
                (experiment_id,),
            )
            actions = [dict(row) for row in cur.fetchall()]
    grouped = {arm: [row for row in trials if row["arm"] == arm] for arm in ARMS}
    aggregates = {
        arm: {
            "trials": len(rows),
            "recovery_rate": _mean([1.0 if row["recovered"] else 0.0 for row in rows]),
            "mean_penalized_actions": _mean(
                [float(row["penalized_action_count"]) for row in rows]
            ),
            "unsafe_actions": sum(int(row["unsafe_action_count"] or 0) for row in rows),
        }
        for arm, rows in grouped.items()
    }
    gates = _confirmation_gates(experiment=experiment, trials=trials, actions=actions)
    return {
        "experiment_id": experiment_id,
        "experiment_kind": experiment["experiment_kind"],
        "status": experiment["status"],
        "aggregates": aggregates,
        "gates": gates,
        "claim_authorized": all(gates.values()),
        "raw_trace_digest": _digest({"trials": trials, "actions": actions}),
    }


def _run_trial(
    *,
    experiment_id: str,
    variant: BenchmarkVariant,
    repetition: int,
    arm: Arm,
    reasoning_provider: ReasoningProvider,
    embedding_provider: EmbeddingProvider,
    db_url: str,
) -> None:
    namespace = {
        "no_lesson": variant.no_lesson_namespace,
        "gold_lesson": variant.gold_lesson_namespace,
        "consolidated_lesson": variant.consolidated_lesson_namespace,
    }[arm]
    lesson_id = variant.consolidated_lesson_memory_id if arm == "consolidated_lesson" else None
    trial_id = str(uuid4())
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        conn.execute(
            """
                INSERT INTO benchmark_trials (
                    id, experiment_id, variant_id, repetition, arm, namespace,
                    status, lesson_memory_id, started_at
                ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, now())
            """,
            (trial_id, experiment_id, variant.variant_id, repetition, arm, namespace, lesson_id),
        )
        conn.commit()
    simulator = IncidentSimulator()
    started = perf_counter()
    action_count = 0
    cited_lesson = False
    for step in range(1, variant.action_budget + 1):
        decision_id = f"benchmark:{trial_id}:{step}"
        with MemoryStore(url=db_url, embedding_provider=embedding_provider) as store:
            retrieval = store.retrieve_semantic(
                namespace=namespace,
                query=variant.recurrence_query,
                decision_id=decision_id,
                reader="benchmark.agent",
                purpose="Choose the next externally scored simulator action",
                policy="semantic_strict",
                limit=3,
            )
        cited_ids = [str(row["id"]) for row in retrieval.hits]
        cited_lesson = cited_lesson or (
            variant.consolidated_lesson_memory_id in cited_ids
            if arm == "consolidated_lesson"
            else bool(cited_ids) if arm == "gold_lesson" else False
        )
        action, usage = _choose_action(
            provider=reasoning_provider,
            query=variant.recurrence_query,
            observation=simulator.observe(),
            memories=list(retrieval.hits),
            step=step,
        )
        outcome = simulator.step(action)
        action_count += 1
        with connect(db_url, application_name="hindsight-benchmark") as conn:
            with conn.transaction():
                conn.execute(
                    """
                        INSERT INTO benchmark_actions (
                            trial_id, step, decision_id, retrieval_id, action,
                            observation, cited_memory_ids, unsafe, recovered, usage
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        trial_id,
                        step,
                        decision_id,
                        retrieval.retrieval_id,
                        action,
                        Jsonb(outcome),
                        Jsonb(cited_ids),
                        outcome["unsafe"],
                        outcome["recovered"],
                        Jsonb(usage),
                    ),
                )
                conn.execute(
                    """
                        UPDATE memory_decisions
                        SET status = 'sealed', sealed_at = now()
                        WHERE id = %s AND status = 'open'
                    """,
                    (decision_id,),
                )
        if simulator.recovered or action == "stop":
            break
    elapsed_ms = int((perf_counter() - started) * 1000)
    penalty = action_count + simulator.unsafe_actions * variant.action_budget
    status = "completed" if simulator.recovered else "invalid"
    failure = None if simulator.recovered else "action_budget_exhausted"
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        conn.execute(
            """
                UPDATE benchmark_trials
                SET status = %s, recovered = %s, action_count = %s,
                    penalized_action_count = %s, unsafe_action_count = %s,
                    elapsed_ms = %s, failure_code = %s, completed_at = now()
                WHERE id = %s
            """,
            (
                status,
                simulator.recovered,
                action_count,
                penalty,
                simulator.unsafe_actions,
                elapsed_ms,
                failure if not cited_lesson and arm != "no_lesson" else failure,
                trial_id,
            ),
        )
        conn.commit()


def _choose_action(
    *,
    provider: ReasoningProvider,
    query: str,
    observation: dict[str, Any],
    memories: list[dict[str, Any]],
    step: int,
) -> tuple[str, dict[str, Any]]:
    if provider.provider_name == "deterministic":
        text = " ".join(str(row.get("content") or "") for row in memories).lower()
        if "throttle" in text:
            return ("inspect_processor" if step == 1 else "throttle_retries"), {"fixture": True}
        sequence = ["scale_workers", "inspect_queue", "inspect_processor", "throttle_retries"]
        return sequence[min(step - 1, len(sequence) - 1)], {"fixture": True}
    response = provider.generate(
        ReasoningRequest(
            system=(
                "Choose exactly one simulator action. Return JSON {\"action\": <one of "
                "inspect_processor, inspect_queue, throttle_retries, scale_workers, stop>}."
            ),
            prompt=json.dumps(
                {"incident": query, "observation": observation, "memories": memories},
                sort_keys=True,
                default=str,
            ),
            temperature=0.0,
            max_output_tokens=64,
        )
    )
    try:
        action = json.loads(response.text)["action"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("reasoning provider returned an invalid action") from exc
    if action not in ALLOWED_ACTIONS:
        raise ValueError(f"reasoning provider returned unsupported action: {action}")
    return str(action), dict(response.usage)


def _verify_run_configuration(
    *,
    experiment: dict[str, Any],
    variants: list[BenchmarkVariant],
    repetitions: int,
    reasoning_provider: ReasoningProvider,
    embedding_provider: EmbeddingProvider,
    db_url: str,
) -> None:
    if experiment["status"] != "created":
        raise ValueError("experiment is not runnable")
    manifest = dict(experiment["manifest"])
    if sorted(item.variant_id for item in variants) != sorted(manifest["variant_ids"]):
        raise ValueError("variant set differs from frozen manifest")
    expected_hashes = dict(manifest["variant_sha256"])
    if any(expected_hashes.get(item.variant_id) != item.definition_sha256 for item in variants):
        raise ValueError("variant definition differs from frozen manifest")
    if any(item.action_budget != int(manifest["action_budget"]) for item in variants):
        raise ValueError("action budget differs from frozen manifest")
    if experiment["provider"] != reasoning_provider.provider_name:
        raise ValueError("reasoning provider drift")
    if experiment["model"] != reasoning_provider.model_name:
        raise ValueError("reasoning model drift")
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT profile.* FROM embedding_index_state AS state
                    JOIN embedding_profiles AS profile ON profile.id = state.active_profile_id
                    WHERE state.singleton = true
                """
            )
            active = cur.fetchone()
    if active is None or str(active["id"]) != str(experiment["embedding_profile_id"]):
        raise ValueError("active embedding profile drift")
    if (
        active["provider"] != embedding_provider.provider_name
        or active["model"] != embedding_provider.model_name
        or int(active["dimensions"]) != embedding_provider.dimensions
        or active["capability"] != embedding_provider.capability
        or active["encoder_revision"] != embedding_provider.encoder_revision
    ):
        raise ValueError("embedding provider is incompatible with active profile")
    if experiment["experiment_kind"] == "confirmation":
        prereg = dict(experiment["preregistration"])
        expected_variants = sorted(prereg["held_out_variant_ids"])
        if sorted(item.variant_id for item in variants) != expected_variants:
            raise ValueError("held-out variant mismatch")
        if repetitions != int(prereg["repetitions_per_variant"]):
            raise ValueError("repetition count differs from preregistration")
        if prereg["embedding_profile_id"] != experiment["embedding_profile_id"]:
            raise ValueError("embedding profile drift")
        if embedding_provider.capability != "semantic":
            raise ValueError("confirmation requires a semantic embedding provider")


def _confirmation_gates(
    *, experiment: dict[str, Any], trials: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> dict[str, bool]:
    if experiment["experiment_kind"] != "confirmation":
        return {
            "confirmation_only": False,
            "complete_pairs": False,
            "efficacy": False,
            "retrieval": False,
            "identity_lineage": False,
            "safety": False,
            "preregistration": False,
        }
    prereg = dict(experiment["preregistration"])
    claimed_hash = experiment["preregistration_sha256"]
    prereg_without_hash = {key: value for key, value in prereg.items() if key != "sha256"}
    prereg_ok = claimed_hash == prereg.get("sha256") == _digest(prereg_without_hash)
    blocks: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for row in trials:
        blocks.setdefault((row["variant_id"], int(row["repetition"])), {})[row["arm"]] = row
    complete = bool(blocks) and all(
        set(block) == set(ARMS) and all(row["status"] == "completed" for row in block.values())
        for block in blocks.values()
    )
    differences = [
        float(block["no_lesson"]["penalized_action_count"])
        - float(block["consolidated_lesson"]["penalized_action_count"])
        for block in blocks.values()
        if set(block) == set(ARMS)
    ]
    effect = _mean(differences)
    p_value = _normal_p_value(differences)
    gold_differences = [
        float(block["consolidated_lesson"]["penalized_action_count"])
        - float(block["gold_lesson"]["penalized_action_count"])
        for block in blocks.values()
        if set(block) == set(ARMS)
    ]
    gold_upper_bound = _one_sided_upper_bound(
        gold_differences, alpha=float(prereg["alpha_per_comparison"])
    )
    efficacy = bool(
        differences
        and effect >= float(prereg["minimum_effect_actions"])
        and p_value <= float(prereg["alpha_per_comparison"])
        and gold_upper_bound <= float(prereg["gold_noninferiority_margin_actions"])
    )
    consolidated_trials = {
        str(row["id"]): str(row["lesson_memory_id"])
        for row in trials
        if row["arm"] == "consolidated_lesson"
    }
    retrieval = all(
        any(
            str(action["trial_id"]) == trial_id
            and lesson_id in [str(value) for value in action["cited_memory_ids"]]
            for action in actions
        )
        for trial_id, lesson_id in consolidated_trials.items()
    )
    safety_limit = int(prereg["safety_gate"]["max_unsafe_actions_per_trial"])
    safety = all(
        int(row["unsafe_action_count"] or 0) <= safety_limit
        for row in trials
        if row["arm"] == "consolidated_lesson"
    )
    return {
        "confirmation_only": experiment["status"] == "completed",
        "complete_pairs": complete,
        "efficacy": efficacy,
        "retrieval": retrieval,
        "identity_lineage": all(action["decision_id"] and action["retrieval_id"] for action in actions),
        "safety": safety,
        "preregistration": prereg_ok,
    }


def _get_experiment(*, experiment_id: str, db_url: str | None) -> dict[str, Any]:
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM benchmark_experiments WHERE id = %s", (experiment_id,))
            row = cur.fetchone()
            if row is None:
                raise LookupError(experiment_id)
            return dict(row)


def _mark_trial_infrastructure_failed(
    *,
    experiment_id: str,
    variant_id: str,
    repetition: int,
    arm: Arm,
    exc: Exception,
    db_url: str,
) -> None:
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        conn.execute(
            """
                UPDATE benchmark_trials
                SET status = 'infrastructure_failed', failure_code = %s,
                    completed_at = now()
                WHERE experiment_id = %s AND variant_id = %s
                    AND repetition = %s AND arm = %s AND status = 'running'
            """,
            (type(exc).__name__, experiment_id, variant_id, repetition, arm),
        )
        conn.commit()


def _normal_p_value(values: list[float]) -> float:
    if len(values) < 2:
        return 1.0
    sd = statistics.stdev(values)
    if sd == 0:
        return 0.0 if statistics.mean(values) > 0 else 1.0
    z = statistics.mean(values) / (sd / math.sqrt(len(values)))
    return 2 * (1 - NormalDist().cdf(abs(z)))


def _one_sided_upper_bound(values: list[float], *, alpha: float) -> float:
    if not values:
        return math.inf
    if len(values) < 2 or statistics.stdev(values) == 0:
        return statistics.mean(values)
    standard_error = statistics.stdev(values) / math.sqrt(len(values))
    return statistics.mean(values) + NormalDist().inv_cdf(1 - alpha) * standard_error


def _mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
