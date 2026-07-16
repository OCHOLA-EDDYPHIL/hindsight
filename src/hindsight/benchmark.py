"""Preregistered three-arm cross-episode learning benchmark."""

from __future__ import annotations

import hashlib
import json
import math
import re
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

Arm = Literal["no_lesson", "reference_lesson", "consolidated_lesson"]
ARMS: tuple[Arm, ...] = ("no_lesson", "reference_lesson", "consolidated_lesson")
HELD_OUT_SELECTION_METHOD = "all_frozen_eligible_sha256_order_v1"
BENCHMARK_REPETITIONS = 2
RETRIEVAL_LIMIT = 4
SIMULATOR_ACTIONS: dict[str, tuple[str, ...]] = {
    "retry_amplification": (
        "inspect_dependency",
        "inspect_queue",
        "throttle_retries",
        "scale_workers",
        "stop",
    ),
    "cache_stampede": (
        "inspect_cache",
        "inspect_origin",
        "coalesce_requests",
        "scale_origin",
        "stop",
    ),
    "connection_leak": (
        "inspect_pool",
        "inspect_transactions",
        "isolate_leak",
        "increase_pool",
        "stop",
    ),
    "hot_partition": (
        "inspect_key_distribution",
        "inspect_partition_load",
        "salt_hot_key",
        "add_consumers",
        "stop",
    ),
    "poison_message": (
        "inspect_failed_payload",
        "inspect_consumer_lag",
        "quarantine_message",
        "add_consumers",
        "stop",
    ),
    "lock_contention": (
        "inspect_lock_graph",
        "inspect_query_latency",
        "terminate_blocker",
        "increase_timeouts",
        "stop",
    ),
}
ALL_SIMULATOR_ACTIONS: tuple[str, ...] = (
    *sorted(
        {
            action
            for actions in SIMULATOR_ACTIONS.values()
            for action in actions
            if action != "stop"
        }
    ),
    "stop",
)
ACTION_RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ALL_SIMULATOR_ACTIONS)},
    },
    "required": ["action"],
    "additionalProperties": False,
}


class ScientificTrialFailure(ValueError):
    """A model decision violated the frozen protocol and is outcome-bearing."""


@dataclass(frozen=True)
class BenchmarkVariant:
    """Frozen source/recurrence pair and lesson bindings."""

    variant_id: str
    simulator_kind: str
    recurrence_query: str
    no_lesson_namespace: str
    reference_lesson_namespace: str
    consolidated_lesson_namespace: str
    reference_lesson_memory_id: str
    consolidated_lesson_memory_id: str
    definition_sha256: str
    action_budget: int = 6


@dataclass(frozen=True)
class PowerPlan:
    """Preregistered paired confirmation sample size in mechanism clusters."""

    repetitions_per_variant: int
    independent_mechanisms_required: int
    alpha_familywise: float
    alpha_per_comparison: float
    power: float
    power_target_effect_actions: float
    pilot_sd: float
    reference_noninferiority_pilot_sd: float
    reference_noninferiority_margin_actions: float


class IncidentSimulator:
    """Deterministic multi-mechanism environment that owns external scoring."""

    def __init__(self, simulator_kind: str = "retry_amplification") -> None:
        if simulator_kind not in SIMULATOR_ACTIONS:
            raise ValueError(f"unsupported simulator kind: {simulator_kind}")
        self.simulator_kind = simulator_kind
        self.recovered = False
        self.unsafe_actions = 0
        self._cause_observed = False
        self._state = {
            "retry_amplification": {
                "timeout_rate": 0.30,
                "queue_depth": 1_000,
                "retry_fanout": 4,
                "workers": 10,
            },
            "cache_stampede": {
                "origin_load": 0.94,
                "cache_hit_ratio": 0.08,
                "synchronized_expiry": True,
                "origin_workers": 10,
            },
            "connection_leak": {
                "pool_utilization": 0.98,
                "waiting_requests": 420,
                "leaked_transactions": 7,
                "pool_size": 40,
            },
            "hot_partition": {
                "write_latency_ms": 1_400,
                "backlog": 860,
                "partition_skew": 0.91,
                "consumers": 8,
            },
            "poison_message": {
                "consumer_lag": 1_200,
                "dead_letter_count": 0,
                "poison_replay_count": 84,
                "consumers": 8,
            },
            "lock_contention": {
                "query_latency_ms": 2_800,
                "waiting_transactions": 190,
                "blocking_transaction_age_seconds": 420,
                "statement_timeout_seconds": 30,
            },
        }[simulator_kind]

    @property
    def allowed_actions(self) -> tuple[str, ...]:
        return ALL_SIMULATOR_ACTIONS

    def observe(self, *, include_simulator_kind: bool = True) -> dict[str, Any]:
        hidden = {
            "retry_amplification": "retry_fanout",
            "cache_stampede": "synchronized_expiry",
            "connection_leak": "leaked_transactions",
            "hot_partition": "partition_skew",
            "poison_message": "poison_replay_count",
            "lock_contention": "blocking_transaction_age_seconds",
        }[self.simulator_kind]
        observation = {
            key: value
            for key, value in self._state.items()
            if self._cause_observed or key != hidden
        }
        result = {**observation, "recovered": self.recovered}
        if include_simulator_kind:
            result = {"simulator_kind": self.simulator_kind, **result}
        return result

    def step(self, action: str) -> dict[str, Any]:
        if action not in self.allowed_actions:
            raise ValueError(
                f"unsupported {self.simulator_kind} simulator action: {action}"
            )
        unsafe = False
        detail = "agent stopped" if action == "stop" else "action did not address active mechanism"
        if self.simulator_kind == "retry_amplification":
            if action == "inspect_dependency":
                self._cause_observed = True
                detail = "downstream retry amplification observed"
            elif action == "inspect_queue":
                detail = "queue pressure observed"
            elif action == "throttle_retries":
                if self._cause_observed:
                    self._state.update(timeout_rate=0.04, queue_depth=180, retry_fanout=1)
                    self.recovered = True
                    detail = "retry fanout throttled; downstream pressure recovered"
                else:
                    detail = "retry control deferred until amplification is diagnosed"
            elif action == "scale_workers":
                self._state["workers"] += 5
                unsafe = self._state["timeout_rate"] >= 0.20
                if unsafe:
                    self._state["queue_depth"] += 500
        elif self.simulator_kind == "cache_stampede":
            if action == "inspect_cache":
                self._cause_observed = True
                detail = "synchronized cache expiry observed"
            elif action == "inspect_origin":
                detail = "origin saturation observed"
            elif action == "coalesce_requests":
                if self._cause_observed:
                    self._state.update(origin_load=0.31, cache_hit_ratio=0.86)
                    self.recovered = True
                    detail = "request coalescing stabilized the origin"
                else:
                    detail = "coalescing deferred until cache behavior is diagnosed"
            elif action == "scale_origin":
                self._state["origin_workers"] += 5
                unsafe = self._state["origin_load"] >= 0.80
        elif self.simulator_kind == "connection_leak":
            if action == "inspect_transactions":
                self._cause_observed = True
                detail = "leaking transactions observed"
            elif action == "inspect_pool":
                detail = "connection pool saturation observed"
            elif action == "isolate_leak":
                if self._cause_observed:
                    self._state.update(
                        pool_utilization=0.42,
                        waiting_requests=20,
                        leaked_transactions=0,
                    )
                    self.recovered = True
                    detail = "leaking workload isolated and connections released"
                else:
                    detail = "isolation deferred until transaction ownership is diagnosed"
            elif action == "increase_pool":
                self._state["pool_size"] += 20
                unsafe = self._state["pool_utilization"] >= 0.90
        elif self.simulator_kind == "hot_partition":
            if action == "inspect_key_distribution":
                self._cause_observed = True
                detail = "hot-key partition skew observed"
            elif action == "inspect_partition_load":
                detail = "partition load observed"
            elif action == "salt_hot_key":
                if self._cause_observed:
                    self._state.update(
                        write_latency_ms=120,
                        backlog=90,
                        partition_skew=0.18,
                    )
                    self.recovered = True
                    detail = "hot key salted; partition load rebalanced"
                else:
                    detail = "key salting deferred until distribution is diagnosed"
            elif action == "add_consumers":
                self._state["consumers"] += 4
                unsafe = self._state["partition_skew"] >= 0.80
        elif self.simulator_kind == "poison_message":
            if action == "inspect_failed_payload":
                self._cause_observed = True
                detail = "repeated poison-message delivery observed"
            elif action == "inspect_consumer_lag":
                detail = "consumer lag and redelivery pressure observed"
            elif action == "quarantine_message":
                if self._cause_observed:
                    self._state.update(
                        consumer_lag=70,
                        dead_letter_count=1,
                        poison_replay_count=0,
                    )
                    self.recovered = True
                    detail = "poison message quarantined; healthy deliveries resumed"
                else:
                    detail = "quarantine deferred until the failing payload is identified"
            elif action == "add_consumers":
                self._state["consumers"] += 4
                unsafe = self._state["poison_replay_count"] > 0
        elif self.simulator_kind == "lock_contention":
            if action == "inspect_lock_graph":
                self._cause_observed = True
                detail = "long-lived blocking transaction observed"
            elif action == "inspect_query_latency":
                detail = "query latency and waiter pressure observed"
            elif action == "terminate_blocker":
                if self._cause_observed:
                    self._state.update(
                        query_latency_ms=140,
                        waiting_transactions=8,
                        blocking_transaction_age_seconds=0,
                    )
                    self.recovered = True
                    detail = "blocking transaction terminated; waiters drained"
                else:
                    detail = "termination deferred until the blocking owner is identified"
            elif action == "increase_timeouts":
                self._state["statement_timeout_seconds"] += 30
                unsafe = self._state["blocking_transaction_age_seconds"] > 0
        if unsafe:
            self.unsafe_actions += 1
            detail = f"{action} amplified unresolved upstream pressure"
        return {**self.observe(), "action": action, "unsafe": unsafe, "detail": detail}


def power_analysis(
    *,
    paired_differences: list[float],
    reference_paired_differences: list[float] | None = None,
    power_target_effect_actions: float = 1.0,
    reference_noninferiority_margin_actions: float = 1.0,
    repetitions_per_variant: int = 2,
    power: float = 0.90,
    alpha_familywise: float = 0.05,
    comparisons: int = 2,
) -> PowerPlan:
    """Power both paired endpoints using mechanism-level pilot effects.

    Repeated runs and same-mechanism incident variants are measurement replicates
    and never increase the independent sample size. The returned mechanism
    requirement is the larger of
    the efficacy and reference-noninferiority calculations, with an additional
    floor that lets the preregistered exact sign-flip tests reach alpha.
    """

    if len(paired_differences) < 4:
        raise ValueError("at least four independent pilot mechanisms are required")
    if power_target_effect_actions <= 0:
        raise ValueError("power_target_effect_actions must be positive")
    if reference_noninferiority_margin_actions <= 0:
        raise ValueError("reference_noninferiority_margin_actions must be positive")
    if repetitions_per_variant != BENCHMARK_REPETITIONS:
        raise ValueError("claim-bearing studies require exactly two repetitions per variant")
    reference_differences = (
        paired_differences
        if reference_paired_differences is None
        else reference_paired_differences
    )
    if len(reference_differences) < 4:
        raise ValueError("at least four independent reference pilot mechanisms are required")
    if not 0 < power < 1:
        raise ValueError("power must be between zero and one")
    if not 0 < alpha_familywise < 1 or comparisons < 1:
        raise ValueError("alpha and comparisons must define a valid test family")
    efficacy_sd = statistics.stdev(paired_differences)
    reference_sd = statistics.stdev(reference_differences)
    alpha = alpha_familywise / comparisons
    z_power = NormalDist().inv_cdf(power)
    efficacy_mechanisms = math.ceil(
        (
            (NormalDist().inv_cdf(1 - alpha) + z_power)
            * max(efficacy_sd, 0.5)
            / power_target_effect_actions
        )
        ** 2
    )
    reference_mechanisms = math.ceil(
        (
            (NormalDist().inv_cdf(1 - alpha) + z_power)
            * max(reference_sd, 0.5)
            / reference_noninferiority_margin_actions
        )
        ** 2
    )
    # A one-sided exact sign-flip test cannot attain alpha below this floor.
    exact_resolution_mechanisms = math.ceil(math.log2(1 / alpha))
    return PowerPlan(
        repetitions_per_variant=repetitions_per_variant,
        independent_mechanisms_required=max(
            2,
            efficacy_mechanisms,
            reference_mechanisms,
            exact_resolution_mechanisms,
        ),
        alpha_familywise=alpha_familywise,
        alpha_per_comparison=alpha,
        power=power,
        power_target_effect_actions=power_target_effect_actions,
        pilot_sd=efficacy_sd,
        reference_noninferiority_pilot_sd=reference_sd,
        reference_noninferiority_margin_actions=reference_noninferiority_margin_actions,
    )


def _power_plan_from_completed_pilot(
    *, pilot: dict[str, Any], rows: list[dict[str, Any]]
) -> PowerPlan:
    manifest = dict(pilot["manifest"])
    pilot_variant_ids = [str(value) for value in manifest["variant_ids"]]
    repetitions = int(manifest["repetitions"])
    if repetitions != BENCHMARK_REPETITIONS:
        raise ValueError("claim-bearing pilot requires exactly two repetitions per variant")
    blocks, complete = _trial_blocks(
        trials=rows,
        expected_variant_ids=pilot_variant_ids,
        repetitions=repetitions,
    )
    if not complete:
        raise ValueError("pilot does not contain the exact completed paired block set")
    differences, reference_differences = _mechanism_level_differences(
        blocks=blocks,
        variant_ids=pilot_variant_ids,
        repetitions=repetitions,
        variant_simulator_kind={
            str(key): str(value)
            for key, value in dict(manifest["variant_simulator_kind"]).items()
        },
    )
    return power_analysis(
        paired_differences=differences,
        reference_paired_differences=reference_differences,
        power_target_effect_actions=1.0,
        reference_noninferiority_margin_actions=1.0,
        repetitions_per_variant=BENCHMARK_REPETITIONS,
        power=0.90,
        alpha_familywise=0.05,
        comparisons=2,
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
    additional_contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a content-addressed confirmation contract before held-out runs."""

    contract = dict(additional_contract or {})
    required_contract_fields = {
        "corpus_schema_version",
        "corpus_sha256",
        "held_out_variant_sha256",
        "embedding_max_distance",
        "retrieval_rank_requirement",
        "arm_context_policy",
        "source_evidence_policy",
        "study_key_sha256",
        "claim_family_sha256",
        "code_sha",
        "variant_query_sha256",
        "variant_simulator_kind",
        "action_vocabulary",
        "action_vocabulary_sha256",
    }
    missing_contract_fields = required_contract_fields - set(contract)
    if missing_contract_fields:
        raise ValueError(
            "confirmation contract fields are missing: "
            + ", ".join(sorted(missing_contract_fields))
        )
    if contract["embedding_max_distance"] is None:
        raise ValueError("confirmation requires a finite embedding distance cutoff")
    if int(contract["retrieval_rank_requirement"]) != 1:
        raise ValueError("confirmation requires the learned lesson at retrieval rank 1")
    if list(contract["action_vocabulary"]) != list(ALL_SIMULATOR_ACTIONS) or str(
        contract["action_vocabulary_sha256"]
    ) != _digest(list(ALL_SIMULATOR_ACTIONS)):
        raise ValueError("confirmation action vocabulary differs from protocol v3")
    if (
        power_plan.repetitions_per_variant != BENCHMARK_REPETITIONS
        or power_plan.alpha_familywise != 0.05
        or power_plan.alpha_per_comparison != 0.025
        or power_plan.power != 0.90
        or power_plan.power_target_effect_actions != 1.0
        or power_plan.reference_noninferiority_margin_actions != 1.0
        or max_unsafe_actions != 0
    ):
        raise ValueError("confirmation power and safety parameters differ from protocol v3")
    if not held_out_variant_ids:
        raise ValueError("held-out variants are required")
    if len(held_out_variant_ids) != len(set(held_out_variant_ids)):
        raise ValueError("held-out variant ids must be unique")
    eligible_variant_ids = sorted(held_out_variant_ids)
    simulator_kinds = {
        str(variant_id): str(simulator_kind)
        for variant_id, simulator_kind in dict(contract["variant_simulator_kind"]).items()
    }
    if set(simulator_kinds) != set(eligible_variant_ids):
        raise ValueError("held-out simulator bindings must exactly cover the eligible pool")
    independent_mechanisms = set(simulator_kinds.values())
    if len(independent_mechanisms) < power_plan.independent_mechanisms_required:
        raise ValueError(
            "insufficient independent held-out mechanisms: "
            f"need {power_plan.independent_mechanisms_required}, "
            f"got {len(independent_mechanisms)}"
        )
    eligible_hashes = dict(contract["held_out_variant_sha256"])
    if set(eligible_hashes) != set(eligible_variant_ids):
        raise ValueError(
            "held-out variant hashes must exactly cover the eligible pool"
        )
    selection_order = _deterministic_held_out_order(
        variant_ids=eligible_variant_ids,
        contract=contract,
    )
    selected_variant_ids = selection_order
    preregistration = {
        "schema_version": 3,
        "pilot_experiment_id": pilot_experiment_id,
        "pilot_excluded_from_confirmation": True,
        "eligible_held_out_variant_ids": eligible_variant_ids,
        "held_out_variant_ids": selected_variant_ids,
        "held_out_selection_method": HELD_OUT_SELECTION_METHOD,
        "arms": list(ARMS),
        "repetitions_per_variant": power_plan.repetitions_per_variant,
        "independent_analysis_unit": "simulator_kind",
        "independent_mechanisms_required": power_plan.independent_mechanisms_required,
        "frozen_independent_mechanisms_available": len(independent_mechanisms),
        "maximum_supported_pilot_sd_actions": (
            math.sqrt(len(independent_mechanisms))
            * power_plan.power_target_effect_actions
            / (
                NormalDist().inv_cdf(1 - power_plan.alpha_per_comparison)
                + NormalDist().inv_cdf(power_plan.power)
            )
        ),
        "repetition_aggregation": "mean_within_variant_then_mean_within_mechanism",
        "paired_blocking": ["simulator_kind", "variant_id", "repetition"],
        "arm_ordering": "sha256_deterministic_permutation_v1",
        "action_vocabulary": list(ALL_SIMULATOR_ACTIONS),
        "action_vocabulary_sha256": _digest(list(ALL_SIMULATOR_ACTIONS)),
        "primary_endpoint": "penalized_action_count",
        "power_target_effect_actions": power_plan.power_target_effect_actions,
        "reference_noninferiority_margin_actions": (
            power_plan.reference_noninferiority_margin_actions
        ),
        "alpha_familywise": power_plan.alpha_familywise,
        "alpha_per_comparison": power_plan.alpha_per_comparison,
        "target_power": power_plan.power,
        "power_method": "normal_approximation_for_exact_mechanism_sign_flip_v1",
        "pilot_efficacy_sd": power_plan.pilot_sd,
        "pilot_reference_noninferiority_sd": power_plan.reference_noninferiority_pilot_sd,
        "pilot_sd_floor_actions": 0.5,
        "multiplicity": "bonferroni_two_comparisons",
        "efficacy_test": "one_sided_exact_mechanism_sign_flip_mean",
        "reference_noninferiority_test": "one_sided_exact_mechanism_sign_flip_mean",
        "minimum_observed_efficacy_actions": power_plan.power_target_effect_actions,
        "safety_gate": {"max_unsafe_actions_per_trial": max_unsafe_actions},
        "retrieval_gate": {
            "reference_and_consolidated_rank_one_rate": 1.0,
            "fallback_allowed": False,
        },
        "identity_lineage_gate": {"complete": True},
        "provider": provider,
        "model": model,
        "embedding_profile_id": embedding_profile_id,
        "additional_contract": contract,
    }
    return {**preregistration, "sha256": _digest(preregistration)}


def preregister_from_completed_pilot(
    *,
    pilot_experiment_id: str,
    held_out_variant_ids: list[str],
    provider: str,
    model: str,
    embedding_profile_id: str,
    power_target_effect_actions: float = 1.0,
    additional_contract: dict[str, Any] | None = None,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Derive variance and sample size only from a completed pilot's paired traces."""

    pilot = _get_experiment(experiment_id=pilot_experiment_id, db_url=db_url)
    if pilot["experiment_kind"] != "pilot" or pilot["status"] != "completed":
        raise ValueError("power analysis requires a completed pilot")
    for field, actual in (
        ("provider", provider),
        ("model", model),
        ("embedding_profile_id", embedding_profile_id),
    ):
        if str(pilot[field]) != str(actual):
            raise ValueError(f"pilot configuration differs from confirmation: {field}")
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT variant_id, repetition, arm, status, penalized_action_count
                    FROM benchmark_trials
                    WHERE experiment_id = %s
                """,
                (pilot_experiment_id,),
            )
            rows = [dict(row) for row in cur.fetchall()]
    pilot_variants = {str(row["variant_id"]) for row in rows}
    overlap = pilot_variants & set(held_out_variant_ids)
    if overlap:
        raise ValueError("held-out variants overlap pilot: " + ", ".join(sorted(overlap)))
    if power_target_effect_actions != 1.0:
        raise ValueError("protocol v3 fixes the power target at one action")
    pilot_manifest = dict(pilot["manifest"])
    frozen_held_out_ids = [
        str(value) for value in pilot_manifest["eligible_held_out_variant_ids"]
    ]
    if set(held_out_variant_ids) != set(frozen_held_out_ids):
        raise ValueError("held-out variants differ from the pilot-frozen eligible pool")
    contract = dict(additional_contract or {})
    for field, pilot_field in (
        ("held_out_variant_sha256", "eligible_held_out_variant_sha256"),
        ("variant_query_sha256", "eligible_held_out_query_sha256"),
        ("variant_simulator_kind", "eligible_held_out_simulator_kind"),
    ):
        if dict(contract.get(field) or {}) != dict(pilot_manifest[pilot_field]):
            raise ValueError(f"held-out contract differs from pilot-frozen pool: {field}")
    plan = _power_plan_from_completed_pilot(pilot=pilot, rows=rows)
    preregistration = preregister_confirmation(
        pilot_experiment_id=pilot_experiment_id,
        held_out_variant_ids=held_out_variant_ids,
        power_plan=plan,
        provider=provider,
        model=model,
        embedding_profile_id=embedding_profile_id,
        additional_contract=contract,
    )
    return _persist_preregistration(
        preregistration=preregistration,
        db_url=db_url,
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

    study_key_sha256 = str(manifest.get("study_key_sha256") or "") or None
    claim_family_sha256 = str(manifest.get("claim_family_sha256") or "") or None
    code_sha = str(manifest.get("code_sha") or "") or None
    if experiment_kind != "ci_smoke" and (
        study_key_sha256 is None or claim_family_sha256 is None or code_sha is None
    ):
        raise ValueError(
            "live experiments require immutable claim-family, study, and code identities"
        )

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
                if study_key_sha256 is not None:
                    cur.execute(
                        """
                            SELECT * FROM benchmark_experiments
                            WHERE study_key_sha256 = %s AND experiment_kind = %s
                                AND status IN ('created', 'running', 'completed', 'failed')
                            FOR UPDATE
                        """,
                        (study_key_sha256, experiment_kind),
                    )
                    existing = cur.fetchone()
                    if existing is not None:
                        if (
                            existing["status"] in {"created", "completed"}
                            and str(existing["manifest_sha256"]) == manifest_hash
                            and (
                                str(existing["preregistration_sha256"] or "")
                                == str(prereg_hash or "")
                            )
                        ):
                            return dict(existing)
                        raise ValueError(
                            "this immutable study already has an experiment attempt"
                        )
                    cur.execute(
                        """
                            SELECT id FROM benchmark_experiments
                            WHERE claim_family_sha256 = %s AND experiment_kind = %s
                                AND status IN ('created', 'running', 'completed', 'failed')
                            FOR UPDATE
                        """,
                        (claim_family_sha256, experiment_kind),
                    )
                    if cur.fetchone() is not None:
                        raise ValueError(
                            "this immutable claim family already has an experiment attempt"
                        )
                    cur.execute(
                        """
                            SELECT candidate.id,
                                EXISTS (
                                    SELECT 1 FROM benchmark_trials AS trial
                                    WHERE trial.experiment_id = candidate.id
                                        AND (
                                            trial.status IN ('completed', 'invalid')
                                            OR trial.penalized_action_count IS NOT NULL
                                            OR EXISTS (
                                                SELECT 1 FROM benchmark_actions AS action
                                                WHERE action.trial_id = trial.id
                                            )
                                        )
                                ) AS outcome_bearing
                            FROM benchmark_experiments AS candidate
                            WHERE candidate.claim_family_sha256 = %s
                                AND candidate.experiment_kind = %s
                                AND candidate.status = 'incomplete'
                            FOR UPDATE
                        """,
                        (claim_family_sha256, experiment_kind),
                    )
                    if any(bool(row["outcome_bearing"]) for row in cur.fetchall()):
                        raise ValueError(
                            "an outcome-bearing incomplete study cannot be replaced"
                        )
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
                            SELECT * FROM benchmark_confirmation_preregistrations
                            WHERE pilot_experiment_id = %s
                            FOR UPDATE
                        """,
                        (preregistration["pilot_experiment_id"],),
                    )
                    prepared = cur.fetchone()
                    if prepared is None:
                        raise ValueError("confirmation preregistration was not prepared durably")
                    if prepared["confirmation_experiment_id"] is not None:
                        cur.execute(
                            """
                                SELECT status FROM benchmark_experiments WHERE id = %s
                            """,
                            (prepared["confirmation_experiment_id"],),
                        )
                        previous_confirmation = cur.fetchone()
                        if (
                            previous_confirmation is None
                            or previous_confirmation["status"] != "incomplete"
                        ):
                            raise ValueError(
                                "this pilot already has a non-replaceable confirmation"
                            )
                    if (
                        str(prepared["preregistration_sha256"])
                        != str(preregistration["sha256"])
                        or dict(prepared["preregistration"]) != dict(preregistration)
                    ):
                        raise ValueError("confirmation differs from the durable preregistration")
                cur.execute(
                    """
                        INSERT INTO benchmark_experiments (
                            id, experiment_kind, manifest, manifest_sha256,
                            preregistration, preregistration_sha256,
                            provider, model, embedding_profile_id,
                            study_key_sha256, claim_family_sha256, code_sha
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
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
                        study_key_sha256,
                        claim_family_sha256,
                        code_sha,
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
                for arm in _deterministic_arm_order(
                    experiment_id=experiment_id,
                    variant_id=variant.variant_id,
                    repetition=repetition,
                ):
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
                    except ScientificTrialFailure as exc:
                        _mark_trial_scientific_failed(
                            experiment_id=experiment_id,
                            variant_id=variant.variant_id,
                            repetition=repetition,
                            arm=arm,
                            exc=exc,
                            db_url=resolved_url,
                        )
                        raise
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
                """
                    UPDATE benchmark_experiments SET status = 'incomplete', completed_at = now()
                    WHERE id = %s AND status != 'failed'
                        AND NOT EXISTS (
                            SELECT 1 FROM benchmark_trials
                            WHERE experiment_id = %s AND status IN ('queued', 'running')
                        )
                """,
                (experiment_id, experiment_id),
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
                    WHERE trial.experiment_id = %s
                    ORDER BY trial.variant_id, trial.repetition, trial.arm,
                        action.trial_id, action.step
                """,
                (experiment_id,),
            )
            actions = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                    SELECT DISTINCT decision.*
                    FROM memory_decisions AS decision
                    JOIN benchmark_actions AS action ON action.decision_id = decision.id
                    JOIN benchmark_trials AS trial ON trial.id = action.trial_id
                    WHERE trial.experiment_id = %s
                    ORDER BY decision.id
                """,
                (experiment_id,),
            )
            decisions = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                    SELECT DISTINCT retrieval.*
                    FROM memory_retrievals AS retrieval
                    JOIN benchmark_actions AS action ON action.retrieval_id = retrieval.id
                    JOIN benchmark_trials AS trial ON trial.id = action.trial_id
                    WHERE trial.experiment_id = %s
                    ORDER BY retrieval.id
                """,
                (experiment_id,),
            )
            retrievals = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                    SELECT read.*
                    FROM memory_reads AS read
                    JOIN benchmark_actions AS action ON action.retrieval_id = read.retrieval_id
                    JOIN benchmark_trials AS trial ON trial.id = action.trial_id
                    WHERE trial.experiment_id = %s
                    ORDER BY read.retrieval_id, read.rank, read.id
                """,
                (experiment_id,),
            )
            reads = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                    SELECT DISTINCT memory.id, memory.content, memory.content_schema,
                        memory.structured_payload
                    FROM semantic_memories AS memory
                    JOIN memory_reads AS read ON read.semantic_memory_id = memory.id
                    JOIN benchmark_actions AS action ON action.retrieval_id = read.retrieval_id
                    JOIN benchmark_trials AS trial ON trial.id = action.trial_id
                    WHERE trial.experiment_id = %s
                    ORDER BY memory.id
                """,
                (experiment_id,),
            )
            read_memories = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                    SELECT DISTINCT memory.id, memory.trust_status, memory.t_invalid,
                        memory.lineage_status
                    FROM semantic_memories AS memory
                    JOIN benchmark_trials AS trial ON trial.lesson_memory_id = memory.id
                    WHERE trial.experiment_id = %s
                    ORDER BY memory.id
                """,
                (experiment_id,),
            )
            target_memories = [dict(row) for row in cur.fetchall()]
            cur.execute(
                """
                    SELECT * FROM benchmark_variant_preparations
                    WHERE experiment_id = %s ORDER BY variant_id
                """,
                (experiment_id,),
            )
            current_preparations = [dict(row) for row in cur.fetchall()]
            claim_family_sha256 = str(
                dict(experiment["manifest"]).get("claim_family_sha256") or ""
            )
            report_preregistration = dict(experiment.get("preregistration") or {})
            bound_pilot_id = report_preregistration.get("pilot_experiment_id") or experiment_id
            cur.execute(
                """
                    SELECT candidate.id, candidate.status,
                        EXISTS (
                            SELECT 1 FROM benchmark_variant_preparations AS preparation
                            WHERE preparation.experiment_id = candidate.id
                                AND preparation.status = 'scientific_failed'
                        ) AS scientific_failure,
                        EXISTS (
                            SELECT 1 FROM benchmark_trials AS trial
                            WHERE trial.experiment_id = candidate.id
                                AND (
                                    trial.status IN ('completed', 'invalid')
                                    OR trial.penalized_action_count IS NOT NULL
                                    OR EXISTS (
                                        SELECT 1 FROM benchmark_actions AS action
                                        WHERE action.trial_id = trial.id
                                    )
                                )
                        ) AS outcome_bearing
                    FROM benchmark_experiments AS candidate
                    WHERE candidate.experiment_kind IN ('pilot', 'confirmation')
                        AND candidate.id != %s AND candidate.id != %s
                        AND candidate.manifest->>'claim_family_sha256' = %s
                    ORDER BY candidate.created_at, candidate.id
                """,
                (experiment_id, bound_pilot_id, claim_family_sha256),
            )
            prior_attempts = [dict(row) for row in cur.fetchall()]
            pilot = None
            pilot_trials: list[dict[str, Any]] = []
            preregistration = report_preregistration
            pilot_experiment_id = preregistration.get("pilot_experiment_id")
            if pilot_experiment_id:
                cur.execute(
                    "SELECT * FROM benchmark_experiments WHERE id = %s",
                    (pilot_experiment_id,),
                )
                pilot_row = cur.fetchone()
                pilot = dict(pilot_row) if pilot_row is not None else None
                cur.execute(
                    """
                        SELECT variant_id, repetition, arm, status, penalized_action_count
                        FROM benchmark_trials WHERE experiment_id = %s
                    """,
                    (pilot_experiment_id,),
                )
                pilot_trials = [dict(row) for row in cur.fetchall()]
                cur.execute(
                    """
                        SELECT binding.*, confirmation.status,
                            EXISTS (
                                SELECT 1 FROM benchmark_variant_preparations AS preparation
                                WHERE preparation.experiment_id = confirmation.id
                                    AND preparation.status = 'scientific_failed'
                            ) AS scientific_failure,
                            EXISTS (
                                SELECT 1 FROM benchmark_trials AS trial
                                WHERE trial.experiment_id = confirmation.id
                                    AND (
                                        trial.status IN ('completed', 'invalid')
                                        OR trial.penalized_action_count IS NOT NULL
                                        OR EXISTS (
                                            SELECT 1 FROM benchmark_actions AS action
                                            WHERE action.trial_id = trial.id
                                        )
                                    )
                            ) AS outcome_bearing
                        FROM benchmark_confirmation_bindings AS binding
                        JOIN benchmark_experiments AS confirmation
                            ON confirmation.id = binding.confirmation_experiment_id
                        WHERE binding.pilot_experiment_id = %s
                        ORDER BY binding.binding_sequence, binding.confirmation_experiment_id
                    """,
                    (pilot_experiment_id,),
                )
                binding_history = [dict(row) for row in cur.fetchall()]
            else:
                binding_history = []
    canonical_preregistration = _canonical_preregistration_for_report(
        experiment=experiment,
        pilot=pilot,
        pilot_trials=pilot_trials,
    )
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
    gates, inference = _confirmation_gates(
        experiment=experiment,
        trials=trials,
        actions=actions,
        decisions=decisions,
        retrievals=retrievals,
        reads=reads,
        read_memories=read_memories,
        target_memories=target_memories,
        prior_attempts=prior_attempts,
        preparations=current_preparations,
        canonical_preregistration=canonical_preregistration,
        binding_history=binding_history,
    )
    raw_trace_digest, claim_evidence_digest = _report_digests(
        experiment_id=experiment_id,
        manifest_sha256=str(experiment["manifest_sha256"]),
        preregistration_sha256=str(experiment["preregistration_sha256"]),
        raw_trace={
            "trials": trials,
            "actions": actions,
            "decisions": decisions,
            "retrievals": retrievals,
            "reads": reads,
            "read_memories": read_memories,
            "preparations": current_preparations,
        },
        target_governance_snapshot=target_memories,
        binding_history=binding_history,
        prior_claim_attempts=prior_attempts,
        gates=gates,
        inference=inference,
    )
    return {
        "experiment_id": experiment_id,
        "experiment_kind": experiment["experiment_kind"],
        "status": experiment["status"],
        "aggregates": aggregates,
        "inference": inference,
        "prior_claim_attempts": prior_attempts,
        "confirmation_binding_history": binding_history,
        "target_governance_snapshot": target_memories,
        "gates": gates,
        "claim_authorized": all(gates.values()),
        "raw_trace_digest": raw_trace_digest,
        "claim_evidence_digest": claim_evidence_digest,
    }


def _report_digests(
    *,
    experiment_id: str,
    manifest_sha256: str,
    preregistration_sha256: str,
    raw_trace: dict[str, Any],
    target_governance_snapshot: list[dict[str, Any]],
    binding_history: list[dict[str, Any]],
    prior_claim_attempts: list[dict[str, Any]],
    gates: dict[str, bool],
    inference: dict[str, Any],
) -> tuple[str, str]:
    """Keep immutable execution identity separate from current governance state."""

    raw_trace_digest = _digest(raw_trace)
    claim_evidence_digest = _digest(
        {
            "experiment_id": experiment_id,
            "manifest_sha256": manifest_sha256,
            "preregistration_sha256": preregistration_sha256,
            "raw_trace_digest": raw_trace_digest,
            "target_governance_snapshot": target_governance_snapshot,
            "binding_history": binding_history,
            "prior_claim_attempts": prior_claim_attempts,
            "gates": gates,
            "inference": inference,
        }
    )
    return raw_trace_digest, claim_evidence_digest


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
        "reference_lesson": variant.reference_lesson_namespace,
        "consolidated_lesson": variant.consolidated_lesson_namespace,
    }[arm]
    lesson_id = {
        "no_lesson": None,
        "reference_lesson": variant.reference_lesson_memory_id,
        "consolidated_lesson": variant.consolidated_lesson_memory_id,
    }[arm]
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
    simulator = IncidentSimulator(variant.simulator_kind)
    started = perf_counter()
    action_count = 0
    target_rank_one = True
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
                limit=RETRIEVAL_LIMIT,
            )
        cited_ids = [str(row["id"]) for row in retrieval.hits]
        if lesson_id is not None:
            target_rank_one = target_rank_one and bool(cited_ids) and cited_ids[0] == lesson_id
        action, usage = _choose_action(
            provider=reasoning_provider,
            query=variant.recurrence_query,
            observation=simulator.observe(include_simulator_kind=False),
            memories=list(retrieval.hits),
            step=step,
            simulator_kind=variant.simulator_kind,
        )
        try:
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
        except ScientificTrialFailure:
            raise
        except Exception as exc:
            raise ScientificTrialFailure(
                "an outcome-bearing simulator action could not be persisted"
            ) from exc
        if simulator.recovered or action == "stop":
            break
    elapsed_ms = int((perf_counter() - started) * 1000)
    penalty = action_count + simulator.unsafe_actions * variant.action_budget
    status = "completed" if simulator.recovered and target_rank_one else "invalid"
    failure = None
    if not target_rank_one:
        failure = "target_lesson_not_rank_one"
    elif not simulator.recovered:
        failure = "action_budget_exhausted"
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
                failure,
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
    simulator_kind: str = "retry_amplification",
) -> tuple[str, dict[str, Any]]:
    blinded_memories = [
        {
            "content": str(row.get("content") or ""),
        }
        for row in memories
    ]
    if provider.provider_name == "deterministic":
        text = " ".join(row["content"] for row in blinded_memories).lower()
        learned_markers = {
            "retry_amplification": "throttle",
            "cache_stampede": "coalesc",
            "connection_leak": "leak",
            "hot_partition": "salt",
            "poison_message": "quarantin",
            "lock_contention": "blocker",
        }
        optimal = {
            "retry_amplification": ("inspect_dependency", "throttle_retries"),
            "cache_stampede": ("inspect_cache", "coalesce_requests"),
            "connection_leak": ("inspect_transactions", "isolate_leak"),
            "hot_partition": ("inspect_key_distribution", "salt_hot_key"),
            "poison_message": ("inspect_failed_payload", "quarantine_message"),
            "lock_contention": ("inspect_lock_graph", "terminate_blocker"),
        }
        unassisted = {
            "retry_amplification": (
                "scale_workers",
                "inspect_queue",
                "inspect_dependency",
                "throttle_retries",
            ),
            "cache_stampede": (
                "scale_origin",
                "inspect_origin",
                "inspect_cache",
                "coalesce_requests",
            ),
            "connection_leak": (
                "increase_pool",
                "inspect_pool",
                "inspect_transactions",
                "isolate_leak",
            ),
            "hot_partition": (
                "add_consumers",
                "inspect_partition_load",
                "inspect_key_distribution",
                "salt_hot_key",
            ),
            "poison_message": (
                "add_consumers",
                "inspect_consumer_lag",
                "inspect_failed_payload",
                "quarantine_message",
            ),
            "lock_contention": (
                "increase_timeouts",
                "inspect_query_latency",
                "inspect_lock_graph",
                "terminate_blocker",
            ),
        }
        if learned_markers[simulator_kind] in text:
            sequence = optimal[simulator_kind]
        else:
            sequence = unassisted[simulator_kind]
        return sequence[min(step - 1, len(sequence) - 1)], {"fixture": True}
    allowed_actions = ALL_SIMULATOR_ACTIONS
    response = provider.generate(
        ReasoningRequest(
            system=(
                "Choose exactly one simulator action. Return JSON with only an action key. "
                f"The allowed actions are: {', '.join(allowed_actions)}."
            ),
            prompt=json.dumps(
                {
                    "incident": query,
                    "observation": observation,
                    "memories": blinded_memories,
                },
                sort_keys=True,
                default=str,
            ),
            temperature=0.0,
            max_output_tokens=256,
            response_json_schema=ACTION_RESPONSE_JSON_SCHEMA,
            thinking_budget=0,
        )
    )
    action = _parse_action_response(response.text)
    if action not in allowed_actions:
        raise ScientificTrialFailure(
            f"reasoning provider returned unsupported action: {action}"
        )
    return str(action), dict(response.usage)


def _parse_action_response(text: str) -> str:
    """Accept bare JSON or one optional JSON fence, but no prose or extra fields."""

    candidate = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        candidate,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced is not None:
        candidate = fenced.group(1).strip()
    try:
        payload = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ScientificTrialFailure("reasoning provider returned an invalid action") from exc
    if not isinstance(payload, dict) or set(payload) != {"action"}:
        raise ScientificTrialFailure("reasoning provider returned an invalid action")
    action = payload["action"]
    if not isinstance(action, str) or not action:
        raise ScientificTrialFailure("reasoning provider returned an invalid action")
    return action


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
    if (
        str(experiment.get("study_key_sha256") or "")
        != str(manifest.get("study_key_sha256") or "")
        or str(experiment.get("claim_family_sha256") or "")
        != str(manifest.get("claim_family_sha256") or "")
        or str(experiment.get("code_sha") or "") != str(manifest.get("code_sha") or "")
    ):
        raise ValueError("experiment identity columns differ from the immutable manifest")
    if sorted(item.variant_id for item in variants) != sorted(manifest["variant_ids"]):
        raise ValueError("variant set differs from frozen manifest")
    if repetitions != int(manifest["repetitions"]):
        raise ValueError("repetition count differs from frozen manifest")
    if experiment["experiment_kind"] != "ci_smoke" and repetitions != BENCHMARK_REPETITIONS:
        raise ValueError("claim-bearing studies require exactly two repetitions")
    if list(manifest.get("action_vocabulary") or []) != list(
        ALL_SIMULATOR_ACTIONS
    ) or str(manifest.get("action_vocabulary_sha256") or "") != _digest(
        list(ALL_SIMULATOR_ACTIONS)
    ):
        raise ValueError("action vocabulary differs from frozen protocol")
    expected_hashes = dict(manifest["variant_sha256"])
    if any(expected_hashes.get(item.variant_id) != item.definition_sha256 for item in variants):
        raise ValueError("variant definition differs from frozen manifest")
    if any(item.action_budget != int(manifest["action_budget"]) for item in variants):
        raise ValueError("action budget differs from frozen manifest")
    expected_queries = dict(manifest["variant_query_sha256"])
    expected_simulators = dict(manifest["variant_simulator_kind"])
    if any(
        expected_queries.get(item.variant_id)
        != hashlib.sha256(item.recurrence_query.encode("utf-8")).hexdigest()
        for item in variants
    ):
        raise ValueError("variant recurrence query differs from frozen manifest")
    if any(
        expected_simulators.get(item.variant_id) != item.simulator_kind for item in variants
    ):
        raise ValueError("variant simulator differs from frozen manifest")
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
            cur.execute(
                """
                    SELECT * FROM benchmark_variant_preparations
                    WHERE experiment_id = %s ORDER BY variant_id
                """,
                (experiment["id"],),
            )
            preparations = [dict(row) for row in cur.fetchall()]
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
    preparation_by_variant = {str(row["variant_id"]): row for row in preparations}
    if set(preparation_by_variant) != {item.variant_id for item in variants}:
        raise ValueError("experiment does not have the exact prepared variant set")
    for item in variants:
        preparation = preparation_by_variant[item.variant_id]
        expected_namespaces = {
            "no_lesson": f"benchmark:{experiment['id']}:{item.variant_id}:arm:no-lesson",
            "reference_lesson": (
                f"benchmark:{experiment['id']}:{item.variant_id}:arm:reference-lesson"
            ),
            "consolidated_lesson": (
                f"benchmark:{experiment['id']}:{item.variant_id}:arm:consolidated-lesson"
            ),
        }
        if (
            preparation["status"] != "completed"
            or str(preparation["definition_sha256"]) != item.definition_sha256
            or str(preparation["reference_memory_id"]) != item.reference_lesson_memory_id
            or str(preparation["consolidated_memory_id"])
            != item.consolidated_lesson_memory_id
            or preparation["incident_id"] is None
            or preparation["source_memory_id"] is None
            or item.no_lesson_namespace != expected_namespaces["no_lesson"]
            or item.reference_lesson_namespace != expected_namespaces["reference_lesson"]
            or item.consolidated_lesson_namespace
            != expected_namespaces["consolidated_lesson"]
        ):
            raise ValueError("variant differs from its durable completed preparation")
    if experiment["experiment_kind"] == "confirmation":
        prereg = dict(experiment["preregistration"])
        if prereg.get("schema_version") != 3:
            raise ValueError("confirmation uses an ineligible legacy protocol")
        expected_variants = sorted(prereg["held_out_variant_ids"])
        if sorted(item.variant_id for item in variants) != expected_variants:
            raise ValueError("held-out variant mismatch")
        if repetitions != int(prereg["repetitions_per_variant"]):
            raise ValueError("repetition count differs from preregistration")
        if prereg["embedding_profile_id"] != experiment["embedding_profile_id"]:
            raise ValueError("embedding profile drift")
        if embedding_provider.capability != "semantic":
            raise ValueError("confirmation requires a semantic embedding provider")
        contract = dict(prereg["additional_contract"])
        if active["max_distance"] is None or float(active["max_distance"]) != float(
            contract["embedding_max_distance"]
        ):
            raise ValueError("embedding distance cutoff differs from preregistration")
        expected_variant_hashes = {
            variant_id: str(contract["held_out_variant_sha256"][variant_id])
            for variant_id in expected_variants
        }
        if {
            key: str(value) for key, value in dict(manifest["variant_sha256"]).items()
        } != expected_variant_hashes:
            raise ValueError("held-out variant hashes differ from preregistration")
        for field in (
            "corpus_schema_version",
            "corpus_sha256",
            "retrieval_rank_requirement",
            "arm_context_policy",
            "source_evidence_policy",
            "study_key_sha256",
            "claim_family_sha256",
            "code_sha",
            "variant_query_sha256",
            "variant_simulator_kind",
            "action_vocabulary",
            "action_vocabulary_sha256",
        ):
            if manifest.get(field) != contract[field]:
                raise ValueError(f"confirmation manifest differs from preregistration: {field}")


def _canonical_preregistration_for_report(
    *,
    experiment: dict[str, Any],
    pilot: dict[str, Any] | None,
    pilot_trials: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Rebuild protocol v3 from immutable pilot traces and fixed constants."""

    try:
        if (
            experiment["experiment_kind"] != "confirmation"
            or pilot is None
            or pilot["experiment_kind"] != "pilot"
            or pilot["status"] != "completed"
        ):
            return None
        manifest = dict(experiment["manifest"])
        pilot_manifest = dict(pilot["manifest"])
        for field in (
            "schema_version",
            "corpus_schema_version",
            "corpus_sha256",
            "provider",
            "model",
            "embedding_profile_id",
            "embedding_max_distance",
            "simulator",
            "action_budget",
            "arms",
            "arm_context_policy",
            "source_evidence_policy",
            "retrieval_rank_requirement",
            "independent_analysis_unit",
            "action_vocabulary",
            "action_vocabulary_sha256",
            "repetitions",
            "study_key_sha256",
            "claim_family_sha256",
            "code_sha",
        ):
            if pilot_manifest.get(field) != manifest.get(field):
                return None
        for field in ("provider", "model", "embedding_profile_id"):
            if str(pilot[field]) != str(experiment[field]):
                return None
        held_out_ids = [
            str(value) for value in pilot_manifest["eligible_held_out_variant_ids"]
        ]
        if set(held_out_ids) != set(str(value) for value in manifest["variant_ids"]):
            return None
        if dict(manifest["variant_sha256"]) != dict(
            pilot_manifest["eligible_held_out_variant_sha256"]
        ):
            return None
        if dict(manifest["variant_query_sha256"]) != dict(
            pilot_manifest["eligible_held_out_query_sha256"]
        ):
            return None
        if dict(manifest["variant_simulator_kind"]) != dict(
            pilot_manifest["eligible_held_out_simulator_kind"]
        ):
            return None
        if set(held_out_ids) & {str(value) for value in pilot_manifest["variant_ids"]}:
            return None
        plan = _power_plan_from_completed_pilot(pilot=pilot, rows=pilot_trials)
        contract = {
            "corpus_schema_version": pilot_manifest["corpus_schema_version"],
            "corpus_sha256": pilot_manifest["corpus_sha256"],
            "held_out_variant_sha256": dict(
                pilot_manifest["eligible_held_out_variant_sha256"]
            ),
            "embedding_max_distance": pilot_manifest["embedding_max_distance"],
            "retrieval_rank_requirement": pilot_manifest["retrieval_rank_requirement"],
            "arm_context_policy": pilot_manifest["arm_context_policy"],
            "source_evidence_policy": pilot_manifest["source_evidence_policy"],
            "study_key_sha256": pilot_manifest["study_key_sha256"],
            "claim_family_sha256": pilot_manifest["claim_family_sha256"],
            "code_sha": pilot_manifest["code_sha"],
            "variant_query_sha256": dict(
                pilot_manifest["eligible_held_out_query_sha256"]
            ),
            "variant_simulator_kind": dict(
                pilot_manifest["eligible_held_out_simulator_kind"]
            ),
            "action_vocabulary": list(pilot_manifest["action_vocabulary"]),
            "action_vocabulary_sha256": pilot_manifest[
                "action_vocabulary_sha256"
            ],
        }
        return preregister_confirmation(
            pilot_experiment_id=str(pilot["id"]),
            held_out_variant_ids=held_out_ids,
            power_plan=plan,
            provider=str(experiment["provider"]),
            model=str(experiment["model"]),
            embedding_profile_id=str(experiment["embedding_profile_id"]),
            max_unsafe_actions=0,
            additional_contract=contract,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _confirmation_gates(
    *,
    experiment: dict[str, Any],
    trials: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    retrievals: list[dict[str, Any]],
    reads: list[dict[str, Any]],
    target_memories: list[dict[str, Any]],
    prior_attempts: list[dict[str, Any]],
    preparations: list[dict[str, Any]],
    read_memories: list[dict[str, Any]],
    canonical_preregistration: dict[str, Any] | None,
    binding_history: list[dict[str, Any]],
) -> tuple[dict[str, bool], dict[str, Any]]:
    if experiment["experiment_kind"] != "confirmation":
        return _false_confirmation_gates(), {}
    prereg = dict(experiment["preregistration"] or {})
    if prereg.get("schema_version") != 3:
        gates = _false_confirmation_gates()
        gates["confirmation_only"] = experiment["status"] == "completed"
        return gates, {"reason": "legacy confirmation protocol is not claim-eligible"}
    claimed_hash = experiment["preregistration_sha256"]
    prereg_without_hash = {key: value for key, value in prereg.items() if key != "sha256"}
    expected_selection = _deterministic_held_out_order(
        variant_ids=[str(value) for value in prereg["eligible_held_out_variant_ids"]],
        contract=dict(prereg["additional_contract"]),
    )
    prereg_ok = bool(
        claimed_hash == prereg.get("sha256") == _digest(prereg_without_hash)
        and canonical_preregistration is not None
        and prereg == canonical_preregistration
        and str(experiment["manifest_sha256"])
        == _digest(dict(experiment["manifest"]))
        and prereg.get("held_out_selection_method") == HELD_OUT_SELECTION_METHOD
        and [str(value) for value in prereg["held_out_variant_ids"]] == expected_selection
        and str(experiment.get("study_key_sha256"))
        == str(prereg["additional_contract"]["study_key_sha256"])
        and str(experiment.get("code_sha"))
        == str(prereg["additional_contract"]["code_sha"])
        and str(experiment.get("claim_family_sha256"))
        == str(prereg["additional_contract"]["claim_family_sha256"])
        and str(dict(experiment["manifest"]).get("claim_family_sha256"))
        == str(prereg["additional_contract"]["claim_family_sha256"])
    )
    expected_variants = [str(value) for value in prereg["held_out_variant_ids"]]
    repetitions = int(prereg["repetitions_per_variant"])
    blocks, complete = _trial_blocks(
        trials=trials,
        expected_variant_ids=expected_variants,
        repetitions=repetitions,
    )
    differences: list[float] = []
    reference_differences: list[float] = []
    if complete:
        differences, reference_differences = _mechanism_level_differences(
            blocks=blocks,
            variant_ids=expected_variants,
            repetitions=repetitions,
            variant_simulator_kind={
                str(key): str(value)
                for key, value in dict(
                    prereg["additional_contract"]["variant_simulator_kind"]
                ).items()
            },
        )
    effect = _mean(differences)
    reference_effect = _mean(reference_differences)
    efficacy_p_value = _exact_sign_flip_p_value(differences, alternative="greater")
    reference_margin = float(prereg["reference_noninferiority_margin_actions"])
    reference_p_value = _exact_sign_flip_p_value(
        [reference_margin - value for value in reference_differences],
        alternative="greater",
    )
    alpha = float(prereg["alpha_per_comparison"])
    enough_independent_mechanisms = len(differences) >= int(
        prereg["independent_mechanisms_required"]
    )
    efficacy = bool(
        complete
        and enough_independent_mechanisms
        and effect >= float(prereg["power_target_effect_actions"])
        and efficacy_p_value <= alpha
    )
    reference_noninferiority = bool(
        complete
        and enough_independent_mechanisms
        and reference_effect < reference_margin
        and reference_p_value <= alpha
    )
    target_trials = {
        str(row["id"]): str(row["lesson_memory_id"])
        for row in trials
        if row["arm"] in {"reference_lesson", "consolidated_lesson"}
        and row.get("lesson_memory_id") is not None
    }
    rank_requirement = int(prereg["additional_contract"]["retrieval_rank_requirement"])
    actions_by_trial: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        actions_by_trial.setdefault(str(action["trial_id"]), []).append(action)
    retrieval = bool(target_trials) and all(
        bool(actions_by_trial.get(trial_id))
        and all(
            [str(value) for value in action["cited_memory_ids"]][:rank_requirement]
            == [lesson_id]
            for action in actions_by_trial[trial_id]
        )
        for trial_id, lesson_id in target_trials.items()
    ) and {
        str(row["id"]) for row in target_memories
    } == set(target_trials.values()) and all(
        row["trust_status"] == "active"
        and row["t_invalid"] is None
        and row["lineage_status"] == "complete"
        for row in target_memories
    )
    safety_limit = int(prereg["safety_gate"]["max_unsafe_actions_per_trial"])
    safety = complete and all(
        int(row["unsafe_action_count"] or 0) <= safety_limit
        for row in trials
        if row["arm"] == "consolidated_lesson"
    )
    identity_lineage = complete and _identity_lineage_complete(
        experiment=experiment,
        trials=trials,
        actions=actions,
        decisions=decisions,
        retrievals=retrievals,
        reads=reads,
    )
    manifest = dict(experiment["manifest"])
    preparation_by_variant = {str(row["variant_id"]): row for row in preparations}
    expected_preparation_variants = {
        str(value) for value in manifest.get("variant_ids") or []
    }
    preparation = bool(expected_preparation_variants) and (
        len(preparations) == len(preparation_by_variant)
        and set(preparation_by_variant) == expected_preparation_variants
        and all(
            row["status"] == "completed"
            and str(row["definition_sha256"])
            == str(dict(manifest["variant_sha256"])[variant_id])
            and row["incident_id"] is not None
            and row["source_memory_id"] is not None
            and row["reference_memory_id"] is not None
            and row["consolidated_memory_id"] is not None
            for variant_id, row in preparation_by_variant.items()
        )
    )
    target_bindings = preparation and _target_bindings_complete(
        experiment_id=str(experiment["id"]),
        trials=trials,
        preparations=preparation_by_variant,
        expected_variant_ids=expected_variants,
        repetitions=repetitions,
    )
    context_parity = target_bindings and _retrieved_context_parity(
        trials=trials,
        actions=actions,
        read_memories=read_memories,
    )
    retrieval = retrieval and target_bindings
    no_prior_scientific_attempt = not any(
        row["status"] == "completed"
        or bool(row["scientific_failure"])
        or bool(row["outcome_bearing"])
        for row in prior_attempts
    )
    binding_history_valid = _binding_history_complete(
        binding_history=binding_history,
        experiment_id=str(experiment["id"]),
        preregistration_sha256=str(experiment["preregistration_sha256"]),
    )
    return (
        {
            "confirmation_only": experiment["status"] == "completed",
            "complete_pairs": complete,
            "independent_sample_size": enough_independent_mechanisms,
            "efficacy": efficacy,
            "reference_noninferiority": reference_noninferiority,
            "retrieval": retrieval,
            "identity_lineage": identity_lineage,
            "preparation": preparation,
            "target_bindings": target_bindings,
            "context_parity": context_parity,
            "safety": safety,
            "preregistration": prereg_ok,
            "no_prior_scientific_attempt": no_prior_scientific_attempt,
            "binding_history": binding_history_valid,
        },
        {
            "analysis_unit": "simulator_kind",
            "independent_mechanisms": len(differences),
            "independent_mechanisms_required": int(
                prereg["independent_mechanisms_required"]
            ),
            "frozen_independent_mechanisms_available": int(
                prereg["frozen_independent_mechanisms_available"]
            ),
            "repetitions_per_variant": repetitions,
            "mean_efficacy_actions": effect,
            "efficacy_exact_p_value": efficacy_p_value,
            "mean_reference_difference_actions": reference_effect,
            "reference_noninferiority_exact_p_value": reference_p_value,
        },
    )


def _binding_history_complete(
    *,
    binding_history: list[dict[str, Any]],
    experiment_id: str,
    preregistration_sha256: str,
) -> bool:
    if not binding_history:
        return False
    if [int(row["binding_sequence"]) for row in binding_history] != list(
        range(1, len(binding_history) + 1)
    ):
        return False
    latest = binding_history[-1]
    if (
        str(latest["confirmation_experiment_id"]) != experiment_id
        or str(latest["preregistration_sha256"]) != preregistration_sha256
    ):
        return False
    return all(
        row["status"] == "incomplete"
        and not bool(row["scientific_failure"])
        and not bool(row["outcome_bearing"])
        and str(row["preregistration_sha256"]) == preregistration_sha256
        for row in binding_history[:-1]
    )


def _target_bindings_complete(
    *,
    experiment_id: str,
    trials: list[dict[str, Any]],
    preparations: dict[str, dict[str, Any]],
    expected_variant_ids: list[str],
    repetitions: int,
) -> bool:
    """Require every arm row to use its prepared namespace and exact lesson ID."""

    expected_count = len(expected_variant_ids) * repetitions * len(ARMS)
    if len(trials) != expected_count:
        return False
    expected_variants = set(expected_variant_ids)
    if set(preparations) != expected_variants:
        return False
    for trial in trials:
        variant_id = str(trial["variant_id"])
        arm = str(trial["arm"])
        if variant_id not in expected_variants or arm not in ARMS:
            return False
        base = f"benchmark:{experiment_id}:{variant_id}:arm"
        expected_namespace = {
            "no_lesson": f"{base}:no-lesson",
            "reference_lesson": f"{base}:reference-lesson",
            "consolidated_lesson": f"{base}:consolidated-lesson",
        }[arm]
        if str(trial["namespace"]) != expected_namespace:
            return False
        preparation = preparations[variant_id]
        expected_lesson = {
            "no_lesson": None,
            "reference_lesson": preparation["reference_memory_id"],
            "consolidated_lesson": preparation["consolidated_memory_id"],
        }[arm]
        if expected_lesson is None:
            if trial.get("lesson_memory_id") is not None:
                return False
        elif str(trial.get("lesson_memory_id")) != str(expected_lesson):
            return False
    target_rows = [row for row in trials if row["arm"] != "no_lesson"]
    return len(target_rows) == len(expected_variant_ids) * repetitions * 2


def _retrieved_context_parity(
    *,
    trials: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    read_memories: list[dict[str, Any]],
) -> bool:
    """Prove every arm received the same ordered non-target memory content."""

    memory_fingerprint = {
        str(row["id"]): _digest(
            {
                "content": row["content"],
                "content_schema": row["content_schema"],
                "structured_payload": row["structured_payload"],
            }
        )
        for row in read_memories
    }
    trial_by_id = {str(row["id"]): row for row in trials}
    context_by_block: dict[tuple[str, int], list[tuple[str, ...]]] = {}
    arms_by_block: dict[tuple[str, int], set[str]] = {}
    for action in actions:
        trial = trial_by_id.get(str(action["trial_id"]))
        if trial is None:
            return False
        lesson_id = (
            str(trial["lesson_memory_id"])
            if trial.get("lesson_memory_id") is not None
            else None
        )
        cited = [str(value) for value in action["cited_memory_ids"]]
        context_ids = [memory_id for memory_id in cited if memory_id != lesson_id]
        if len(context_ids) < 2 or any(item not in memory_fingerprint for item in context_ids):
            return False
        fingerprints = tuple(memory_fingerprint[item] for item in context_ids)
        key = (str(trial["variant_id"]), int(trial["repetition"]))
        context_by_block.setdefault(key, []).append(fingerprints)
        arms_by_block.setdefault(key, set()).add(str(trial["arm"]))
    if not context_by_block:
        return False
    return all(
        arms_by_block[key] == set(ARMS)
        and len(set(contexts)) == 1
        for key, contexts in context_by_block.items()
    )


def _false_confirmation_gates() -> dict[str, bool]:
    return {
        "confirmation_only": False,
        "complete_pairs": False,
        "independent_sample_size": False,
        "efficacy": False,
        "reference_noninferiority": False,
        "retrieval": False,
        "identity_lineage": False,
        "preparation": False,
        "target_bindings": False,
        "context_parity": False,
        "safety": False,
        "preregistration": False,
        "no_prior_scientific_attempt": False,
        "binding_history": False,
    }


def _trial_blocks(
    *,
    trials: list[dict[str, Any]],
    expected_variant_ids: list[str],
    repetitions: int,
) -> tuple[dict[tuple[str, int], dict[str, dict[str, Any]]], bool]:
    expected_keys = {
        (variant_id, repetition)
        for variant_id in expected_variant_ids
        for repetition in range(1, repetitions + 1)
    }
    blocks: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for row in trials:
        key = (str(row["variant_id"]), int(row["repetition"]))
        arm = str(row["arm"])
        if arm in blocks.setdefault(key, {}):
            return blocks, False
        blocks[key][arm] = row
    complete = (
        bool(expected_keys)
        and set(blocks) == expected_keys
        and len(trials) == len(expected_keys) * len(ARMS)
        and all(
            set(block) == set(ARMS)
            and all(
                row["status"] == "completed" and row.get("penalized_action_count") is not None
                for row in block.values()
            )
            for block in blocks.values()
        )
    )
    return blocks, complete


def _variant_level_differences(
    *,
    blocks: dict[tuple[str, int], dict[str, dict[str, Any]]],
    variant_ids: list[str],
    repetitions: int,
) -> tuple[list[float], list[float]]:
    efficacy: list[float] = []
    reference_noninferiority: list[float] = []
    for variant_id in variant_ids:
        arm_means = {
            arm: _mean(
                [
                    float(blocks[(variant_id, repetition)][arm]["penalized_action_count"])
                    for repetition in range(1, repetitions + 1)
                ]
            )
            for arm in ARMS
        }
        efficacy.append(arm_means["no_lesson"] - arm_means["consolidated_lesson"])
        reference_noninferiority.append(
            arm_means["consolidated_lesson"] - arm_means["reference_lesson"]
        )
    return efficacy, reference_noninferiority


def _mechanism_level_differences(
    *,
    blocks: dict[tuple[str, int], dict[str, dict[str, Any]]],
    variant_ids: list[str],
    repetitions: int,
    variant_simulator_kind: dict[str, str],
) -> tuple[list[float], list[float]]:
    """Aggregate repeated incidents before treating mechanisms as independent."""

    if set(variant_simulator_kind) != set(variant_ids):
        raise ValueError("simulator bindings must exactly cover the analyzed variants")
    variant_efficacy, variant_reference = _variant_level_differences(
        blocks=blocks,
        variant_ids=variant_ids,
        repetitions=repetitions,
    )
    efficacy_by_mechanism: dict[str, list[float]] = {}
    reference_by_mechanism: dict[str, list[float]] = {}
    for variant_id, efficacy, reference in zip(
        variant_ids, variant_efficacy, variant_reference, strict=True
    ):
        mechanism = variant_simulator_kind[variant_id]
        efficacy_by_mechanism.setdefault(mechanism, []).append(efficacy)
        reference_by_mechanism.setdefault(mechanism, []).append(reference)
    mechanisms = sorted(efficacy_by_mechanism)
    return (
        [_mean(efficacy_by_mechanism[item]) for item in mechanisms],
        [_mean(reference_by_mechanism[item]) for item in mechanisms],
    )


def _identity_lineage_complete(
    *,
    experiment: dict[str, Any],
    trials: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    retrievals: list[dict[str, Any]],
    reads: list[dict[str, Any]],
) -> bool:
    if not actions:
        return False
    trial_by_id = {str(row["id"]): row for row in trials}
    decision_by_id = {str(row["id"]): row for row in decisions}
    retrieval_by_id = {str(row["id"]): row for row in retrievals}
    manifest = dict(experiment["manifest"])
    query_hashes = {
        str(key): str(value)
        for key, value in dict(manifest.get("variant_query_sha256") or {}).items()
    }
    reads_by_retrieval: dict[str, list[dict[str, Any]]] = {}
    for row in reads:
        reads_by_retrieval.setdefault(str(row["retrieval_id"]), []).append(row)
    actions_by_trial: dict[str, list[dict[str, Any]]] = {}
    for action in actions:
        trial_id = str(action["trial_id"])
        if trial_id not in trial_by_id:
            return False
        actions_by_trial.setdefault(trial_id, []).append(action)
    for trial_id, trial in trial_by_id.items():
        trial_actions = sorted(actions_by_trial.get(trial_id, []), key=lambda row: int(row["step"]))
        action_count = int(trial.get("action_count") or 0)
        if len(trial_actions) != action_count:
            return False
        if [int(row["step"]) for row in trial_actions] != list(range(1, action_count + 1)):
            return False
        for action in trial_actions:
            decision_id = str(action["decision_id"])
            retrieval_id = str(action["retrieval_id"])
            if decision_id != f"benchmark:{trial_id}:{int(action['step'])}":
                return False
            decision = decision_by_id.get(decision_id)
            retrieval = retrieval_by_id.get(retrieval_id)
            if decision is None or retrieval is None:
                return False
            if (
                decision["actor"] != "benchmark.agent"
                or decision["decision_kind"] != "memory_retrieval"
                or decision["purpose"] != "Choose the next externally scored simulator action"
                or decision["status"] != "sealed"
                or str(decision.get("namespace")) != str(trial["namespace"])
                or str(retrieval["decision_id"]) != decision_id
                or str(retrieval["namespace"]) != str(trial["namespace"])
                or retrieval["reader"] != "benchmark.agent"
                or retrieval["purpose"]
                != "Choose the next externally scored simulator action"
                or retrieval["policy"] != "semantic_strict"
                or int(retrieval["policy_version"]) != 1
                or str(retrieval["query_sha256"])
                != query_hashes.get(str(trial["variant_id"]))
                or int(retrieval["requested_limit"]) != RETRIEVAL_LIMIT
                or str(retrieval["embedding_profile_id"])
                != str(experiment["embedding_profile_id"])
            ):
                return False
            cited = [str(value) for value in action["cited_memory_ids"]]
            returned = [str(value) for value in retrieval["returned_memory_ids"]]
            if cited != returned:
                return False
            attempts = list(retrieval["attempts"])
            if len(attempts) != 1 or attempts[0].get("strategy") != "semantic_vector":
                return False
            expected_outcome = "selected" if cited else "empty"
            if (
                attempts[0].get("outcome") != expected_outcome
                or int(attempts[0].get("result_count", -1)) != len(cited)
                or attempts[0].get("error_code") is not None
            ):
                return False
            if cited:
                if (
                    retrieval["status"] != "succeeded"
                    or retrieval["selected_strategy"] != "semantic_vector"
                ):
                    return False
            elif retrieval["status"] != "empty" or retrieval["selected_strategy"] is not None:
                return False
            retrieval_reads = sorted(
                reads_by_retrieval.get(retrieval_id, []),
                key=lambda row: int(row["rank"]),
            )
            if [int(row["rank"]) for row in retrieval_reads] != list(
                range(1, len(cited) + 1)
            ):
                return False
            if [str(row["memory_id"]) for row in retrieval_reads] != cited:
                return False
            if any(
                row["memory_kind"] != "semantic"
                or str(row["semantic_memory_id"]) != str(row["memory_id"])
                or str(row["decision_id"]) != decision_id
                or row["reader"] != "benchmark.agent"
                or row["purpose"] != "Choose the next externally scored simulator action"
                for row in retrieval_reads
            ):
                return False
    return len(decision_by_id) == len(actions) == len(retrieval_by_id)


def _exact_sign_flip_p_value(
    values: list[float], *, alternative: Literal["two-sided", "greater"]
) -> float:
    """Return an exact paired randomization p-value for the arithmetic mean.

    Every sign assignment is retained, including assignments of zero-valued
    pairs, so discrete ties do not silently reduce the independent sample.
    """

    if not values:
        return 1.0
    if len(values) > 20:
        raise ValueError("exact sign-flip inference is limited to 20 independent variants")
    if alternative not in {"greater", "two-sided"}:
        raise ValueError(f"unsupported sign-flip alternative: {alternative}")
    observed = sum(values)
    tolerance = 1e-12
    extreme = 0
    assignments = 1 << len(values)
    for mask in range(assignments):
        permuted = sum(
            value if mask & (1 << index) else -value
            for index, value in enumerate(values)
        )
        if alternative == "greater":
            extreme += permuted >= observed - tolerance
        else:
            extreme += abs(permuted) >= abs(observed) - tolerance
    return extreme / assignments


def _deterministic_arm_order(
    *, experiment_id: str, variant_id: str, repetition: int
) -> tuple[Arm, ...]:
    """Return a reproducible random-looking arm permutation for one paired block."""

    return tuple(
        sorted(
            ARMS,
            key=lambda arm: hashlib.sha256(
                f"{experiment_id}:{variant_id}:{repetition}:{arm}".encode("utf-8")
            ).digest(),
        )
    )


def _deterministic_held_out_order(
    *, variant_ids: list[str], contract: dict[str, Any]
) -> list[str]:
    """Order a frozen eligible pool without using outcome-bearing pilot data."""

    corpus_sha256 = str(contract["corpus_sha256"])
    variant_hashes = dict(contract["held_out_variant_sha256"])
    if set(variant_ids) != set(variant_hashes):
        raise ValueError("held-out variant hashes must exactly cover the eligible pool")
    return sorted(
        variant_ids,
        key=lambda variant_id: (
            hashlib.sha256(
                (
                    f"{HELD_OUT_SELECTION_METHOD}\0{corpus_sha256}\0"
                    f"{variant_id}\0{variant_hashes[variant_id]}"
                ).encode("utf-8")
            ).digest(),
            variant_id,
        ),
    )


def _persist_preregistration(
    *, preregistration: dict[str, Any], db_url: str | None
) -> dict[str, Any]:
    """Persist one immutable, idempotent confirmation contract per pilot."""

    pilot_experiment_id = str(preregistration["pilot_experiment_id"])
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        INSERT INTO benchmark_confirmation_preregistrations (
                            pilot_experiment_id, preregistration,
                            preregistration_sha256
                        ) VALUES (%s, %s, %s)
                        ON CONFLICT (pilot_experiment_id) DO NOTHING
                        RETURNING *
                    """,
                    (
                        pilot_experiment_id,
                        Jsonb(preregistration),
                        preregistration["sha256"],
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                            SELECT * FROM benchmark_confirmation_preregistrations
                            WHERE pilot_experiment_id = %s
                            FOR UPDATE
                        """,
                        (pilot_experiment_id,),
                    )
                    row = cur.fetchone()
                if row is None:
                    raise RuntimeError("durable preregistration could not be loaded")
                stored = dict(row["preregistration"])
                if (
                    str(row["preregistration_sha256"]) != str(preregistration["sha256"])
                    or stored != preregistration
                ):
                    raise ValueError("this pilot already has a different preregistration")
                return stored


def _get_experiment(*, experiment_id: str, db_url: str | None) -> dict[str, Any]:
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM benchmark_experiments WHERE id = %s", (experiment_id,))
            row = cur.fetchone()
            if row is None:
                raise LookupError(experiment_id)
            return dict(row)


def finalize_interrupted_experiments(
    *,
    code_sha: str,
    reason: str,
    db_url: str | None = None,
) -> dict[str, Any]:
    """Terminalize nonterminal traces left by a lost benchmark runner.

    The finalizer never deletes or rewrites completed evidence. Child leases and
    traces are closed before their parent experiment so database guards preserve
    a fail-closed, auditable partial attempt.
    """

    if not code_sha.strip():
        raise ValueError("code_sha is required")
    if not reason.strip():
        raise ValueError("reason is required")
    counts = {
        "experiments": 0,
        "preparations": 0,
        "trials": 0,
        "decisions": 0,
        "consolidation_jobs": 0,
    }
    experiment_ids: list[str] = []
    with connect(db_url, application_name="hindsight-benchmark-finalizer") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                        SELECT id, status
                        FROM benchmark_experiments
                        WHERE code_sha = %s AND status IN ('created', 'running')
                        ORDER BY created_at, id
                        FOR UPDATE
                    """,
                    (code_sha,),
                )
                experiments = [dict(row) for row in cur.fetchall()]
                for experiment in experiments:
                    experiment_id = str(experiment["id"])
                    experiment_ids.append(experiment_id)
                    if experiment["status"] == "created":
                        cur.execute(
                            """
                                SELECT DISTINCT incident_id
                                FROM benchmark_variant_preparations
                                WHERE experiment_id = %s
                                    AND incident_id IS NOT NULL
                                    AND status IN ('queued', 'leased', 'retrying')
                            """,
                            (experiment_id,),
                        )
                        incident_ids = [row["incident_id"] for row in cur.fetchall()]
                        if incident_ids:
                            cur.execute(
                                """
                                    SELECT decision_id
                                    FROM consolidation_jobs
                                    WHERE incident_id = ANY(%s)
                                        AND status IN ('queued', 'leased', 'retrying')
                                        AND decision_id IS NOT NULL
                                """,
                                (incident_ids,),
                            )
                            decision_ids = [str(row["decision_id"]) for row in cur.fetchall()]
                            if decision_ids:
                                cur.execute(
                                    """
                                        UPDATE memory_decisions
                                        SET status = 'failed', sealed_at = COALESCE(sealed_at, now())
                                        WHERE id = ANY(%s) AND status = 'open'
                                    """,
                                    (decision_ids,),
                                )
                                counts["decisions"] += cur.rowcount
                            cur.execute(
                                """
                                    UPDATE consolidation_jobs
                                    SET status = 'failed', lease_owner = NULL,
                                        lease_expires_at = NULL,
                                        error_code = 'BenchmarkRunnerInterrupted',
                                        error_detail = %s, completed_at = now(), updated_at = now()
                                    WHERE incident_id = ANY(%s)
                                        AND status IN ('queued', 'leased', 'retrying')
                                """,
                                (reason, incident_ids),
                            )
                            counts["consolidation_jobs"] += cur.rowcount
                        cur.execute(
                            """
                                UPDATE benchmark_variant_preparations
                                SET status = 'infrastructure_failed',
                                    lease_owner = NULL, lease_expires_at = NULL,
                                    failure_class = 'infrastructure',
                                    failure_code = 'BenchmarkRunnerInterrupted',
                                    failure_detail = %s, completed_at = now(), updated_at = now()
                                WHERE experiment_id = %s
                                    AND status IN ('queued', 'leased', 'retrying')
                            """,
                            (reason, experiment_id),
                        )
                        counts["preparations"] += cur.rowcount
                    else:
                        cur.execute(
                            """
                                SELECT decision.id
                                FROM memory_decisions AS decision
                                JOIN benchmark_trials AS trial
                                    ON decision.id LIKE
                                        'benchmark:' || trial.id::STRING || ':%%'
                                WHERE trial.experiment_id = %s
                                    AND decision.status = 'open'
                            """,
                            (experiment_id,),
                        )
                        decision_ids = [str(row["id"]) for row in cur.fetchall()]
                        if decision_ids:
                            cur.execute(
                                """
                                    UPDATE memory_decisions
                                    SET status = 'failed', sealed_at = COALESCE(sealed_at, now())
                                    WHERE status = 'open' AND id = ANY(%s)
                                """,
                                (decision_ids,),
                            )
                            counts["decisions"] += cur.rowcount
                        cur.execute(
                            """
                                UPDATE benchmark_trials
                                SET status = 'infrastructure_failed',
                                    failure_code = 'BenchmarkRunnerInterrupted',
                                    completed_at = now()
                                WHERE experiment_id = %s AND status IN ('queued', 'running')
                            """,
                            (experiment_id,),
                        )
                        counts["trials"] += cur.rowcount
                    cur.execute(
                        """
                            UPDATE benchmark_experiments
                            SET status = 'incomplete', completed_at = now()
                            WHERE id = %s AND status IN ('created', 'running')
                        """,
                        (experiment_id,),
                    )
                    counts["experiments"] += cur.rowcount
    return {"code_sha": code_sha, "experiment_ids": experiment_ids, **counts}


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
        with conn.transaction():
            trial = conn.execute(
                """
                    UPDATE benchmark_trials
                    SET status = 'infrastructure_failed', failure_code = %s,
                        completed_at = now()
                    WHERE experiment_id = %s AND variant_id = %s
                        AND repetition = %s AND arm = %s AND status = 'running'
                    RETURNING id
                """,
                (type(exc).__name__, experiment_id, variant_id, repetition, arm),
            ).fetchone()
            if trial is not None:
                conn.execute(
                    """
                        UPDATE memory_decisions
                        SET status = 'failed', sealed_at = now()
                        WHERE status = 'open' AND id LIKE %s
                    """,
                    (f"benchmark:{trial[0]}:%",),
                )


def _mark_trial_scientific_failed(
    *,
    experiment_id: str,
    variant_id: str,
    repetition: int,
    arm: Arm,
    exc: Exception,
    db_url: str,
) -> None:
    with connect(db_url, application_name="hindsight-benchmark") as conn:
        with conn.transaction():
            trial = conn.execute(
                """
                    UPDATE benchmark_trials
                    SET status = 'invalid', failure_code = %s, completed_at = now()
                    WHERE experiment_id = %s AND variant_id = %s
                        AND repetition = %s AND arm = %s AND status = 'running'
                    RETURNING id
                """,
                (type(exc).__name__, experiment_id, variant_id, repetition, arm),
            ).fetchone()
            if trial is not None:
                conn.execute(
                    """
                        UPDATE memory_decisions
                        SET status = 'failed', sealed_at = now()
                        WHERE status = 'open' AND id LIKE %s
                    """,
                    (f"benchmark:{trial[0]}:%",),
                )
            conn.execute(
                """
                    UPDATE benchmark_experiments
                    SET status = 'failed', completed_at = now()
                    WHERE id = %s AND status = 'running'
                """,
                (experiment_id,),
            )


def _mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
