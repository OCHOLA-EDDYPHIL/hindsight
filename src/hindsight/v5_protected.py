"""Frozen protocol and immutable evidence primitives for the V5 protected study."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import pathlib
import re
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from statistics import NormalDist
from typing import Any, Literal, Protocol

from hindsight.v5_corpus import (
    ACTION_BUDGET,
    ALL_ACTIONS,
    EMBEDDING_CAPABILITY,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_ENCODER_REVISION,
    EMBEDDING_MAX_DISTANCE,
    EMBEDDING_MODEL,
    EMBEDDING_PROFILE_ID,
    EMBEDDING_PROVIDER,
    EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256,
    GEMINI_PROVIDER_REPRESENTATION,
    MECHANISM_FAMILIES,
    REASONING_MODEL,
    REASONING_PROVIDER,
    applicability_matches,
    compile_scenario,
    development_scenarios,
    sha256_hex,
    validate_scenario,
)
from hindsight.v5_governance import (
    POLICY_REVISION,
    V2_MAXIMUM_DISTANCE_DELTA,
    governance_v2_policy,
)


PROTECTED_PROTOCOL_SCHEMA_VERSION = 1
PROTECTED_PROTOCOL_REVISION = "v5-protected-study-v3"
PROTECTED_PROTOCOL_V2_SHA256 = "7cebae7ffe73f81da25f28150c7c7c4aed9f5b4921ff23b422113de38d95950c"
PROTECTED_SEED_DOMAIN = "hindsight-v5-protected-seed-v1"
PROTECTED_ARM_ORDER_DOMAIN = "hindsight-v5-protected-arm-order-v1"
PROTECTED_REVIEW_DOMAIN = "hindsight-v5-protected-review-v1"
PROTECTED_MINIMUM_SCENARIOS = 36
PROTECTED_MAXIMUM_SCENARIOS = 120
PROTECTED_REPETITIONS = 2
PROTECTED_ARMS = ("no_lesson", "reference_lesson", "consolidated_lesson")
PROTECTED_ALPHA_FAMILYWISE = 0.05
PROTECTED_ALPHA_PER_TEST = 0.025
PROTECTED_POWER = 0.90
PROTECTED_TARGET_EFFECT_ACTIONS = 1.0
PROTECTED_REFERENCE_MARGIN_ACTIONS = 1.0
PROTECTED_SD_FLOOR_ACTIONS = 0.5
PROTECTED_RESERVE_FRACTION = 0.25
PROTECTED_REASONING_TIMEOUT_SECONDS = 60
SIGNATURE_ALGORITHM = "AWS_KMS_HMAC_SHA_256"
PILOT_RESULT_KIND = "v5-protected-pilot-v1"
PILOT_ATTEMPT_AUTHORIZATION_KIND = "v5-protected-pilot-attempt-authorization-v1"
FINAL_FREEZE_KIND = "v5-protected-freeze-v1"
CORPUS_SEAL_KIND = "v5-protected-corpus-v1"
EMBEDDING_CHECKPOINT_KIND = "v5-protected-embedding-checkpoint-v1"
RETRIEVAL_RESULT_KIND = "v5-protected-retrieval-v1"
TERMINAL_RESULT_KIND = "v5-protected-terminal-v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_CODE_SHA_RE = re.compile(r"[0-9a-f]{40}")
_HEX_RE = re.compile(r"[0-9a-f]+")
ACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": list(ALL_ACTIONS)}},
    "required": ["action"],
    "additionalProperties": False,
}
ACTION_SYSTEM_PROMPT = (
    "Choose exactly one next simulator action from the incident, current observation, "
    "and supplied context. Treat the observation as feedback. Do not repeat a previous "
    "action unchanged when the incident is not recovered. A diagnostic action may be "
    "required before remediation. Return JSON with only an action key. Allowed actions: "
    + ", ".join(ALL_ACTIONS)
    + "."
)


class ProtectedStudySigner(Protocol):
    key_id: str

    def token(self, *, kind: str, raw_id: str) -> str: ...


def protected_study_protocol() -> dict[str, Any]:
    """Return the complete protocol frozen before protected construction."""

    body = {
        "schema_version": PROTECTED_PROTOCOL_SCHEMA_VERSION,
        "revision": PROTECTED_PROTOCOL_REVISION,
        "source_governance_revision": POLICY_REVISION,
        "source_governance_policy_sha256": governance_v2_policy()["policy_sha256"],
        "development_rehearsal_selection_sha256": (EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256),
        "providers": {
            "reasoning": {
                "provider": REASONING_PROVIDER,
                "model": REASONING_MODEL,
                "temperature": 0.0,
                "thinking_budget": 0,
                "maximum_transient_attempts": 4,
                "request_timeout_seconds": PROTECTED_REASONING_TIMEOUT_SECONDS,
            },
            "embedding": {
                "provider": EMBEDDING_PROVIDER,
                "model": EMBEDDING_MODEL,
                "dimensions": EMBEDDING_DIMENSIONS,
                "capability": EMBEDDING_CAPABILITY,
                "encoder_revision": EMBEDDING_ENCODER_REVISION,
                "provider_representation": GEMINI_PROVIDER_REPRESENTATION,
                "profile_id": EMBEDDING_PROFILE_ID,
            },
        },
        "arms": {
            "names": list(PROTECTED_ARMS),
            "repetitions": PROTECTED_REPETITIONS,
            "order": "sha256-permutation-v1",
            "no_lesson": "governed-distractors-without-target",
            "reference_lesson": "direct-oracle-derived-positive-control",
            "consolidated_lesson": "strict-retrieval-of-source-derived-governed-memory",
            "non_target_context": "identical-across-arms",
        },
        "reasoning_prompt": {
            "system_sha256": hashlib.sha256(ACTION_SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "response_schema_sha256": sha256_hex(ACTION_RESPONSE_SCHEMA),
        },
        "scoring": {
            "action_budget": ACTION_BUDGET,
            "penalized_action_count": (
                "action_count + action_budget * unsafe_action_count + "
                "action_budget * int(not recovered)"
            ),
            "efficacy_difference": "no_lesson - consolidated_lesson",
            "reference_difference": "consolidated_lesson - reference_lesson",
            "intent_to_treat": True,
        },
        "power": {
            "minimum_scenarios": PROTECTED_MINIMUM_SCENARIOS,
            "maximum_scenarios": PROTECTED_MAXIMUM_SCENARIOS,
            "target_power": PROTECTED_POWER,
            "alpha_familywise": PROTECTED_ALPHA_FAMILYWISE,
            "alpha_per_test": PROTECTED_ALPHA_PER_TEST,
            "target_effect_actions": PROTECTED_TARGET_EFFECT_ACTIONS,
            "reference_margin_actions": PROTECTED_REFERENCE_MARGIN_ACTIONS,
            "sd_floor_actions": PROTECTED_SD_FLOOR_ACTIONS,
            "equal_family_quotas": True,
            "rounding": "ceiling-to-multiple-of-six",
        },
        "inference": {
            "analysis_unit": "scenario",
            "repetition_aggregation": "mean-within-scenario-arm",
            "test": "one-sided-exact-paired-sign-flip-dynamic-programming-v1",
            "minimum_mean_efficacy_actions": PROTECTED_TARGET_EFFECT_ACTIONS,
            "reference_noninferiority_margin_actions": (PROTECTED_REFERENCE_MARGIN_ACTIONS),
        },
        "protected_seed": {
            "source": "nist-randomness-beacon-v2",
            "not_before": "freeze-receipt-time-plus-ten-minutes",
            "domain": PROTECTED_SEED_DOMAIN,
            "reserve_fraction": PROTECTED_RESERVE_FRACTION,
            "reserve_rounding": "ceiling-per-family",
        },
        "retrieval": {
            "policy": "semantic_strict",
            "rank_requirement": 1,
            "semantic_rank_one_acceptance": {
                "scope": "aggregate-all-scenarios",
                "minimum_numerator": 9,
                "minimum_denominator": 10,
            },
            "max_distance": EMBEDDING_MAX_DISTANCE,
            "maximum_distance_delta": V2_MAXIMUM_DISTANCE_DELTA,
            "fallback": None,
            "reranking": False,
            "all_scenarios_required": True,
        },
        "execution": {
            "single_attempt": True,
            "embedding_mode": "cache-only",
            "reasoning_timeout_failure": "monitoring_outage",
            "response_checkpoint_before_action": True,
            "protected_outcome_inspection_during_run": False,
            "rollback_on": [
                "hard_gate_failure",
                "integrity_failure",
                "monitoring_unavailable",
            ],
        },
    }
    return {**body, "protocol_sha256": sha256_hex(body)}


def build_pilot_attempt_authorization(
    *,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    source_protected_authorization_sha256: str,
    prior_pilot_artifact_sha256: str,
    prior_pilot_run_id: str,
    prior_pilot_exact_code_sha: str,
    authorized_runner_sha: str,
    issued_at: str,
    signer: ProtectedStudySigner,
) -> dict[str, Any]:
    """Authorize one recovery pilot after a verified infrastructure rollback."""

    for value, label in (
        (tested_subject_sha, "tested subject"),
        (policy_evaluator_sha, "policy evaluator"),
        (prior_pilot_exact_code_sha, "prior pilot code"),
        (authorized_runner_sha, "authorized runner"),
    ):
        _require_code_sha(value, label)
    for value, label in (
        (source_protected_authorization_sha256, "protected authorization"),
        (prior_pilot_artifact_sha256, "prior pilot artifact"),
    ):
        _require_sha256(value, label)
    issued_at = _timestamp(issued_at, "pilot attempt authorization").isoformat()
    if not prior_pilot_run_id.strip():
        raise ValueError("v5 protected prior pilot run identity is missing")
    body = {
        "schema_version": 1,
        "status": "pilot_attempt_authorized",
        "claim_authorized": True,
        "single_attempt": True,
        "tested_subject_sha": tested_subject_sha,
        "policy_evaluator_sha": policy_evaluator_sha,
        "source_protected_authorization_sha256": (source_protected_authorization_sha256),
        "prior_pilot_artifact_sha256": prior_pilot_artifact_sha256,
        "prior_pilot_run_id": prior_pilot_run_id,
        "prior_pilot_exact_code_sha": prior_pilot_exact_code_sha,
        "prior_pilot_terminal_reason": "artifact_integrity_failure",
        "authorized_runner_sha": authorized_runner_sha,
        "protocol_sha256": protected_study_protocol()["protocol_sha256"],
        "reasoning_timeout_seconds": PROTECTED_REASONING_TIMEOUT_SECONDS,
        "embedding_mode": "cache-only",
        "issued_at": issued_at,
    }
    return sign_protected_artifact(
        body,
        signer=signer,
        kind=PILOT_ATTEMPT_AUTHORIZATION_KIND,
    )


def validate_pilot_attempt_authorization(
    authorization: Mapping[str, Any],
    *,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    source_protected_authorization_sha256: str,
    exact_code_sha: str,
) -> dict[str, Any]:
    """Fail closed unless a signed recovery authorization binds this exact runner."""

    if (
        authorization.get("status") != "pilot_attempt_authorized"
        or authorization.get("claim_authorized") is not True
        or authorization.get("single_attempt") is not True
        or authorization.get("tested_subject_sha") != tested_subject_sha
        or authorization.get("policy_evaluator_sha") != policy_evaluator_sha
        or authorization.get("source_protected_authorization_sha256")
        != source_protected_authorization_sha256
        or authorization.get("authorized_runner_sha") != exact_code_sha
        or authorization.get("protocol_sha256") != protected_study_protocol()["protocol_sha256"]
        or authorization.get("reasoning_timeout_seconds") != PROTECTED_REASONING_TIMEOUT_SECONDS
        or authorization.get("embedding_mode") != "cache-only"
    ):
        raise ValueError("v5 protected pilot attempt authorization differs")
    _require_sha256(
        str(authorization.get("prior_pilot_artifact_sha256") or ""),
        "prior pilot artifact",
    )
    _require_code_sha(
        str(authorization.get("prior_pilot_exact_code_sha") or ""),
        "prior pilot code",
    )
    if not str(authorization.get("prior_pilot_run_id") or "").strip():
        raise ValueError("v5 protected prior pilot run identity is missing")
    _timestamp(authorization.get("issued_at"), "pilot attempt authorization")
    if authorization.get("prior_pilot_terminal_reason") != "artifact_integrity_failure":
        raise ValueError("v5 protected prior pilot was not an infrastructure rollback")
    return dict(authorization)


def power_plan_from_pilot(
    *, efficacy_differences: Sequence[float], reference_differences: Sequence[float]
) -> dict[str, Any]:
    """Compute the predeclared balanced protected sample size from the pilot."""

    if len(efficacy_differences) != 60 or len(reference_differences) != 60:
        raise ValueError("v5 protected power requires all 60 development pilot scenarios")
    efficacy = [_finite(value, "pilot efficacy difference") for value in efficacy_differences]
    reference = [_finite(value, "pilot reference difference") for value in reference_differences]
    efficacy_sd = statistics.stdev(efficacy)
    reference_sd = statistics.stdev(reference)
    z_alpha = NormalDist().inv_cdf(1 - PROTECTED_ALPHA_PER_TEST)
    z_power = NormalDist().inv_cdf(PROTECTED_POWER)

    def required(sd: float, effect: float) -> int:
        return math.ceil(((z_alpha + z_power) * max(sd, PROTECTED_SD_FLOOR_ACTIONS) / effect) ** 2)

    raw_required = max(
        PROTECTED_MINIMUM_SCENARIOS,
        required(efficacy_sd, PROTECTED_TARGET_EFFECT_ACTIONS),
        required(reference_sd, PROTECTED_REFERENCE_MARGIN_ACTIONS),
    )
    balanced_required = math.ceil(raw_required / len(MECHANISM_FAMILIES)) * len(MECHANISM_FAMILIES)
    status = (
        "power_plan_frozen"
        if balanced_required <= PROTECTED_MAXIMUM_SCENARIOS
        else "power_plan_infeasible"
    )
    body = {
        "schema_version": 1,
        "status": status,
        "protocol_sha256": protected_study_protocol()["protocol_sha256"],
        "pilot_scenario_count": len(efficacy),
        "efficacy_sd": efficacy_sd,
        "reference_noninferiority_sd": reference_sd,
        "unrounded_required_scenarios": raw_required,
        "protected_scenario_count": balanced_required,
        "scenarios_per_family": balanced_required // len(MECHANISM_FAMILIES),
        "reserve_per_family": math.ceil(
            (balanced_required // len(MECHANISM_FAMILIES)) * PROTECTED_RESERVE_FRACTION
        ),
        "target_power": PROTECTED_POWER,
        "alpha_per_test": PROTECTED_ALPHA_PER_TEST,
        "target_effect_actions": PROTECTED_TARGET_EFFECT_ACTIONS,
        "reference_margin_actions": PROTECTED_REFERENCE_MARGIN_ACTIONS,
        "sd_floor_actions": PROTECTED_SD_FLOOR_ACTIONS,
    }
    return {**body, "power_plan_sha256": sha256_hex(body)}


def build_behavioral_pilot_result(
    *,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    protected_authorization_sha256: str,
    attempt_authorization_sha256: str,
    rehearsal_result_sha256: str,
    trials: Sequence[Mapping[str, Any]],
    scenario_ids: Sequence[str],
    signer: ProtectedStudySigner,
) -> dict[str, Any]:
    """Seal the complete 60-scenario development pilot and its power plan."""

    _require_code_sha(tested_subject_sha, "tested subject")
    _require_code_sha(policy_evaluator_sha, "policy evaluator")
    _require_sha256(protected_authorization_sha256, "protected authorization")
    _require_sha256(attempt_authorization_sha256, "pilot attempt authorization")
    _require_sha256(rehearsal_result_sha256, "rehearsal result")
    if (
        len(scenario_ids) != 60
        or len(set(scenario_ids)) != 60
        or sha256_hex(list(scenario_ids)) != EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256
    ):
        raise ValueError("v5 protected pilot selection differs")
    efficacy, reference = paired_differences_from_trials(
        trials=trials,
        expected_scenario_ids=scenario_ids,
    )
    power_plan = power_plan_from_pilot(
        efficacy_differences=efficacy,
        reference_differences=reference,
    )
    body = {
        "schema_version": 1,
        "status": (
            "behavioral_pilot_passed"
            if power_plan["status"] == "power_plan_frozen"
            else "behavioral_pilot_infeasible"
        ),
        "tested_subject_sha": tested_subject_sha,
        "policy_evaluator_sha": policy_evaluator_sha,
        "protected_authorization_sha256": protected_authorization_sha256,
        "attempt_authorization_sha256": attempt_authorization_sha256,
        "rehearsal_result_sha256": rehearsal_result_sha256,
        "protocol_sha256": protected_study_protocol()["protocol_sha256"],
        "scenario_ids": list(scenario_ids),
        "scenario_count": len(scenario_ids),
        "trial_count": len(trials),
        "trials": [dict(row) for row in trials],
        "trials_sha256": sha256_hex([dict(row) for row in trials]),
        "efficacy_differences": efficacy,
        "reference_differences": reference,
        "power_plan": power_plan,
        "power_plan_sha256": power_plan["power_plan_sha256"],
    }
    return sign_protected_artifact(body, signer=signer, kind=PILOT_RESULT_KIND)


def build_final_freeze(
    *,
    tested_subject_sha: str,
    policy_evaluator_sha: str,
    protected_runner_sha: str,
    source_protected_authorization_sha256: str,
    source_pilot_sha256: str,
    power_plan: Mapping[str, Any],
    recorded_at: str,
    signer: ProtectedStudySigner,
) -> dict[str, Any]:
    """Bind the complete protected design to one exact runner revision."""

    for value, label in (
        (tested_subject_sha, "tested subject"),
        (policy_evaluator_sha, "policy evaluator"),
        (protected_runner_sha, "protected runner"),
    ):
        _require_code_sha(value, label)
    for value, label in (
        (source_protected_authorization_sha256, "protected authorization"),
        (source_pilot_sha256, "pilot"),
    ):
        _require_sha256(value, label)
    _validate_power_plan(power_plan)
    frozen_at = _timestamp(recorded_at, "freeze receipt").isoformat()
    protocol = protected_study_protocol()
    body = {
        "schema_version": 1,
        "status": "study_frozen",
        "tested_subject_sha": tested_subject_sha,
        "policy_evaluator_sha": policy_evaluator_sha,
        "protected_runner_sha": protected_runner_sha,
        "source_protected_authorization_sha256": source_protected_authorization_sha256,
        "source_pilot_sha256": source_pilot_sha256,
        "recorded_at": frozen_at,
        "protocol": protocol,
        "protocol_sha256": protocol["protocol_sha256"],
        "power_plan": dict(power_plan),
        "power_plan_sha256": power_plan["power_plan_sha256"],
        "beacon_not_before_offset_seconds": 600,
    }
    return sign_protected_artifact(body, signer=signer, kind=FINAL_FREEZE_KIND)


def validate_beacon_receipt(
    receipt: Mapping[str, Any], *, freeze_recorded_at: datetime
) -> dict[str, Any]:
    """Require the first recorded NIST pulse at the frozen not-before time."""

    if freeze_recorded_at.tzinfo is None:
        raise ValueError("v5 protected freeze time must include a timezone")
    if receipt.get("source") != "nist-randomness-beacon-v2":
        raise ValueError("v5 protected seed requires NIST Beacon v2")
    pulse_uri = str(receipt.get("pulse_uri") or "")
    previous_uri = str(receipt.get("previous_pulse_uri") or "")
    if not pulse_uri.startswith("https://beacon.nist.gov/beacon/2.0/pulse/time/"):
        raise ValueError("v5 protected pulse URI is invalid")
    if not previous_uri.startswith("https://beacon.nist.gov/beacon/2.0/pulse/time/"):
        raise ValueError("v5 protected previous pulse URI is invalid")
    value = str(receipt.get("output_value") or "").lower()
    signature = str(receipt.get("signature_value") or "")
    if len(value) not in {64, 128} or not _HEX_RE.fullmatch(value):
        raise ValueError("v5 protected pulse output is invalid")
    if not signature or not _HEX_RE.fullmatch(signature.lower()):
        raise ValueError("v5 protected pulse signature is absent")
    verification = receipt.get("signature_verification")
    if not isinstance(verification, Mapping) or verification.get("verified") is not True:
        raise ValueError("v5 protected pulse signature is not verified")
    if verification.get("method") != "nist-beacon-v2-reference-verifier":
        raise ValueError("v5 protected pulse signature verifier differs")
    _require_sha256(str(verification.get("verifier_sha256") or ""), "beacon verifier")
    _require_sha256(str(verification.get("certificate_sha256") or ""), "beacon certificate")
    pulse_at = _timestamp(receipt.get("published_at"), "pulse publication")
    previous_at = _timestamp(receipt.get("previous_published_at"), "previous pulse publication")
    not_before = freeze_recorded_at.astimezone(UTC) + timedelta(minutes=10)
    if pulse_at < not_before or previous_at >= not_before or previous_at >= pulse_at:
        raise ValueError("v5 protected pulse is not the first pulse at the frozen boundary")
    raw_sha = str(receipt.get("raw_response_sha256") or "")
    previous_raw_sha = str(receipt.get("previous_raw_response_sha256") or "")
    _require_sha256(raw_sha, "beacon response")
    _require_sha256(previous_raw_sha, "previous beacon response")
    return dict(receipt)


def derive_protected_corpus(
    *, final_freeze: Mapping[str, Any], beacon: Mapping[str, Any]
) -> dict[str, Any]:
    """Create balanced primary and reserve scenarios without touching development."""

    if final_freeze.get("status") != "study_frozen":
        raise ValueError("v5 protected corpus requires a frozen study")
    beacon = validate_beacon_receipt(
        beacon,
        freeze_recorded_at=_timestamp(final_freeze.get("recorded_at"), "freeze receipt"),
    )
    tested_subject_sha = str(final_freeze.get("tested_subject_sha") or "")
    _require_code_sha(tested_subject_sha, "tested subject")
    power_plan = final_freeze.get("power_plan")
    if not isinstance(power_plan, Mapping):
        raise ValueError("v5 protected freeze has no power plan")
    _validate_power_plan(power_plan)
    count = int(power_plan["protected_scenario_count"])
    per_family = int(power_plan["scenarios_per_family"])
    reserve_per_family = int(power_plan["reserve_per_family"])
    if count != per_family * len(MECHANISM_FAMILIES):
        raise ValueError("v5 protected family quotas are incomplete")
    beacon_value = str(beacon.get("output_value") or "").lower()
    if len(beacon_value) not in {64, 128} or not _HEX_RE.fullmatch(beacon_value):
        raise ValueError("v5 protected corpus beacon output is invalid")
    freeze_sha = str(final_freeze.get("artifact_sha256") or "")
    _require_sha256(freeze_sha, "final freeze")
    root = hashlib.sha256(
        "\x1f".join((PROTECTED_SEED_DOMAIN, freeze_sha, beacon_value)).encode("utf-8")
    ).hexdigest()
    primary: dict[str, list[dict[str, Any]]] = {}
    reserve: dict[str, list[dict[str, Any]]] = {}
    for family in MECHANISM_FAMILIES:
        family_rows = []
        total = per_family + reserve_per_family
        for index in range(total):
            seed = hashlib.sha256(
                "\x1f".join((PROTECTED_SEED_DOMAIN, root, family, str(index))).encode("utf-8")
            ).hexdigest()
            scenario = compile_scenario(family=family, seed=seed, code_sha=tested_subject_sha)
            validate_scenario(scenario)
            family_rows.append(scenario)
        primary[family] = family_rows[:per_family]
        reserve[family] = family_rows[per_family:]
    scenario_ids = [
        row["scenario_id"]
        for group in (primary, reserve)
        for family in MECHANISM_FAMILIES
        for row in group[family]
    ]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("v5 protected scenario identities are not unique")
    development_ids = {
        row["scenario_id"] for row in development_scenarios(code_sha=tested_subject_sha)
    }
    if development_ids.intersection(scenario_ids):
        raise ValueError("v5 protected scenarios overlap development")
    body = {
        "schema_version": 1,
        "status": "protected_corpus_constructed",
        "final_freeze_sha256": freeze_sha,
        "beacon": dict(beacon),
        "seed_root_sha256": root,
        "primary": primary,
        "reserve": reserve,
        "primary_count": count,
        "reserve_count": reserve_per_family * len(MECHANISM_FAMILIES),
        "scenario_order_sha256": sha256_hex(scenario_ids),
    }
    return {**body, "corpus_sha256": sha256_hex(body)}


def new_review_state(*, corpus: Mapping[str, Any], owner: str) -> dict[str, Any]:
    """Create the ordered append-only owner-review state."""

    if not owner.strip():
        raise ValueError("v5 protected owner identity is required")
    _validate_corpus(corpus)
    slots = []
    for family in MECHANISM_FAMILIES:
        for index, scenario in enumerate(corpus["primary"][family], start=1):
            slots.append(
                {
                    "slot_id": f"{family}:{index}",
                    "family": family,
                    "current_scenario_id": scenario["scenario_id"],
                    "status": "pending",
                    "replacement_count": 0,
                }
            )
    body = {
        "schema_version": 1,
        "status": "review_in_progress",
        "corpus_sha256": corpus["corpus_sha256"],
        "owner": owner,
        "slots": slots,
        "reserve_offsets": {family: 0 for family in MECHANISM_FAMILIES},
        "events": [],
    }
    return {**body, "review_state_sha256": sha256_hex(body)}


def next_review_item(
    *, corpus: Mapping[str, Any], state: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Return the next protected item without experimental outcomes."""

    _validate_review_binding(corpus=corpus, state=state)
    slot = next((row for row in state["slots"] if row["status"] == "pending"), None)
    if slot is None:
        return None
    scenario = _scenario_by_id(corpus, str(slot["current_scenario_id"]))
    target = _applicable_target(scenario)
    oracle = scenario["oracle"]
    recurrence = scenario["agent_view"]["recurrence"]
    return {
        "slot_id": slot["slot_id"],
        "scenario_id": scenario["scenario_id"],
        "incident": recurrence["incident"],
        "visible_evidence": recurrence["initial_observation"],
        "diagnosis": oracle["hidden_causal_mechanism"]["diagnosis"],
        "diagnostic_action": oracle["unique_optimal_actions"][0],
        "recovery_action": oracle["recovery_action"],
        "lesson": target["content"],
    }


def owner_review_guide() -> dict[str, Any]:
    """Return the frozen plain-language criteria shown with every owner item."""

    return {
        "approve_when": [
            "The visible evidence supports the stated diagnosis.",
            "The diagnostic and recovery actions are safe, specific, and ordered.",
            "The lesson accurately states when the source-derived guidance applies.",
        ],
        "reject_when": [
            "The diagnosis conflicts with the incident or visible evidence.",
            "The recovery is ambiguous, unsafe, or missing a required diagnostic step.",
            "The lesson leaks an answer-bearing role or does not follow from the source episode.",
        ],
        "clarification_rule": (
            "Clarification may explain the simulator oracle only; it cannot edit the item "
            "or reveal experimental outcomes."
        ),
    }


def record_review_event(
    *,
    corpus: Mapping[str, Any],
    state: Mapping[str, Any],
    action: Literal["approve", "reject", "clarify"],
    scenario_id: str,
    recorded_at: str,
) -> dict[str, Any]:
    """Append one irreversible review event and promote reserves on rejection."""

    _validate_review_binding(corpus=corpus, state=state)
    if action not in {"approve", "reject", "clarify"}:
        raise ValueError("v5 protected review action is invalid")
    current = next((row for row in state["slots"] if row["status"] == "pending"), None)
    if current is None or current["current_scenario_id"] != scenario_id:
        raise ValueError("v5 protected review must follow the frozen order")
    _timestamp(recorded_at, "review event")
    updated = json.loads(json.dumps(state))
    updated.pop("review_state_sha256", None)
    slot = next(row for row in updated["slots"] if row["slot_id"] == current["slot_id"])
    previous_event_sha = updated["events"][-1]["event_sha256"] if updated["events"] else None
    event_body = {
        "sequence": len(updated["events"]) + 1,
        "slot_id": slot["slot_id"],
        "scenario_id": scenario_id,
        "action": action,
        "recorded_at": recorded_at,
        "previous_event_sha256": previous_event_sha,
    }
    event = {**event_body, "event_sha256": sha256_hex(event_body)}
    updated["events"].append(event)
    if action == "approve":
        slot["status"] = "approved"
    elif action == "reject":
        family = str(slot["family"])
        offset = int(updated["reserve_offsets"][family])
        reserves = corpus["reserve"][family]
        if offset >= len(reserves):
            slot["status"] = "reserve_exhausted"
            updated["status"] = "review_failed"
        else:
            slot["current_scenario_id"] = reserves[offset]["scenario_id"]
            slot["replacement_count"] = int(slot["replacement_count"]) + 1
            updated["reserve_offsets"][family] = offset + 1
    if updated["status"] == "review_in_progress" and all(
        row["status"] == "approved" for row in updated["slots"]
    ):
        updated["status"] = "review_complete"
    return {**updated, "review_state_sha256": sha256_hex(updated)}


def seal_reviewed_corpus(
    *, corpus: Mapping[str, Any], state: Mapping[str, Any], signer: ProtectedStudySigner
) -> dict[str, Any]:
    """Seal the exact approved primary set and review chain."""

    _validate_review_binding(corpus=corpus, state=state)
    if state.get("status") != "review_complete":
        raise ValueError("v5 protected owner review is not complete")
    selected = [
        _scenario_by_id(corpus, str(slot["current_scenario_id"])) for slot in state["slots"]
    ]
    body = {
        "schema_version": 1,
        "status": "protected_corpus_sealed",
        "source_corpus_sha256": corpus["corpus_sha256"],
        "final_freeze_sha256": corpus["final_freeze_sha256"],
        "review_state_sha256": state["review_state_sha256"],
        "owner": state["owner"],
        "selected_scenarios": selected,
        "selected_scenario_ids": [row["scenario_id"] for row in selected],
        "selected_scenario_count": len(selected),
        "unused_reserve_sha256": [
            sha256_hex(row)
            for family in MECHANISM_FAMILIES
            for row in corpus["reserve"][family][int(state["reserve_offsets"][family]) :]
        ],
    }
    return sign_protected_artifact(body, signer=signer, kind=CORPUS_SEAL_KIND)


def deterministic_arm_order(
    *, final_freeze_sha256: str, scenario_id: str, repetition: int
) -> tuple[str, ...]:
    """Return the frozen random-looking arm order for one paired block."""

    _require_sha256(final_freeze_sha256, "final freeze")
    if repetition not in {1, 2}:
        raise ValueError("v5 protected studies require exactly two repetitions")
    return tuple(
        sorted(
            PROTECTED_ARMS,
            key=lambda arm: hashlib.sha256(
                "\x1f".join(
                    (
                        PROTECTED_ARM_ORDER_DOMAIN,
                        final_freeze_sha256,
                        scenario_id,
                        str(repetition),
                        arm,
                    )
                ).encode("utf-8")
            ).digest(),
        )
    )


def reference_lesson(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """Return the direct oracle-derived positive control frozen by the protocol."""

    oracle = scenario.get("oracle")
    if not isinstance(oracle, Mapping):
        raise ValueError("v5 protected reference control requires an oracle")
    actions = list(oracle.get("unique_optimal_actions") or [])
    if len(actions) != 2 or actions[1] != oracle.get("recovery_action"):
        raise ValueError("v5 protected reference action sequence differs")
    return {
        "kind": "direct_reference_control",
        "diagnosis": oracle["hidden_causal_mechanism"]["diagnosis"],
        "diagnostic_action": actions[0],
        "recovery_action": actions[1],
    }


def paired_differences_from_trials(
    *, trials: Sequence[Mapping[str, Any]], expected_scenario_ids: Sequence[str]
) -> tuple[list[float], list[float]]:
    """Validate the complete block set and return the two paired endpoints."""

    expected_keys = {
        (scenario_id, repetition, arm)
        for scenario_id in expected_scenario_ids
        for repetition in range(1, PROTECTED_REPETITIONS + 1)
        for arm in PROTECTED_ARMS
    }
    rows: dict[tuple[str, int, str], Mapping[str, Any]] = {}
    for trial in trials:
        key = (
            str(trial.get("scenario_id") or ""),
            int(trial.get("repetition") or 0),
            str(trial.get("arm") or ""),
        )
        if key in rows:
            raise ValueError("v5 protected trial block contains duplicates")
        rows[key] = trial
    if set(rows) != expected_keys:
        raise ValueError("v5 protected trial block set is incomplete")
    efficacy = []
    reference = []
    for scenario_id in expected_scenario_ids:
        means = {
            arm: statistics.mean(
                _finite(
                    rows[(scenario_id, repetition, arm)].get("penalized_action_count"),
                    "penalized action count",
                )
                for repetition in range(1, PROTECTED_REPETITIONS + 1)
            )
            for arm in PROTECTED_ARMS
        }
        efficacy.append(means["no_lesson"] - means["consolidated_lesson"])
        reference.append(means["consolidated_lesson"] - means["reference_lesson"])
    return efficacy, reference


def evaluate_terminal_result(
    *,
    final_freeze: Mapping[str, Any],
    sealed_corpus: Mapping[str, Any],
    trials: Sequence[Mapping[str, Any]],
    hard_gates_passed: bool,
    rollback_state: str,
    embedding_cache_miss_count: int,
) -> dict[str, Any]:
    """Evaluate complete protected outcomes without offering a tuning path."""

    scenario_ids = list(sealed_corpus.get("selected_scenario_ids") or [])
    efficacy, reference = paired_differences_from_trials(
        trials=trials, expected_scenario_ids=scenario_ids
    )
    mean_efficacy = statistics.mean(efficacy)
    mean_reference = statistics.mean(reference)
    efficacy_p = exact_sign_flip_p_value(efficacy)
    reference_p = exact_sign_flip_p_value(
        [PROTECTED_REFERENCE_MARGIN_ACTIONS - value for value in reference]
    )
    statistical_pass = (
        mean_efficacy >= PROTECTED_TARGET_EFFECT_ACTIONS
        and efficacy_p <= PROTECTED_ALPHA_PER_TEST
        and mean_reference < PROTECTED_REFERENCE_MARGIN_ACTIONS
        and reference_p <= PROTECTED_ALPHA_PER_TEST
    )
    integrity_pass = (
        hard_gates_passed and rollback_state == "disarmed" and embedding_cache_miss_count == 0
    )
    status = "scientific_passed" if statistical_pass and integrity_pass else "scientific_failed"
    return {
        "schema_version": 1,
        "status": status,
        "claim_authorized": status == "scientific_passed",
        "final_freeze_sha256": final_freeze["artifact_sha256"],
        "sealed_corpus_sha256": sealed_corpus["artifact_sha256"],
        "scenario_count": len(scenario_ids),
        "trial_count": len(trials),
        "mean_efficacy_actions": mean_efficacy,
        "efficacy_exact_p_value": efficacy_p,
        "mean_reference_difference_actions": mean_reference,
        "reference_noninferiority_exact_p_value": reference_p,
        "all_hard_gates_passed": hard_gates_passed,
        "embedding_cache_miss_count": embedding_cache_miss_count,
        "rollback_state": rollback_state,
    }


def validate_terminal_artifact(
    *,
    terminal: Mapping[str, Any],
    final_freeze: Mapping[str, Any],
    sealed_corpus: Mapping[str, Any],
) -> dict[str, Any]:
    """Independently recompute a completed result or validate a closed rollback."""

    if terminal.get("status") == "rolled_back":
        if (
            terminal.get("claim_authorized") is not False
            or terminal.get("rollback_state") != "executed"
            or terminal.get("rollback_reason")
            not in {
                "safety_failure",
                "hard_gate_failure",
                "tenant_isolation_breach",
                "embedding_cache_miss",
                "invalid_signature",
                "artifact_integrity_failure",
                "audit_integrity_failure",
                "monitoring_outage",
                "audit_sink_unavailable",
            }
        ):
            raise ValueError("v5 protected rollback terminal is invalid")
        return dict(terminal)
    trials = terminal.get("trials")
    if not isinstance(trials, list) or terminal.get("trials_sha256") != sha256_hex(trials):
        raise ValueError("v5 protected terminal trial evidence differs")
    recomputed = evaluate_terminal_result(
        final_freeze=final_freeze,
        sealed_corpus=sealed_corpus,
        trials=trials,
        hard_gates_passed=terminal.get("all_hard_gates_passed") is True,
        rollback_state=str(terminal.get("rollback_state") or ""),
        embedding_cache_miss_count=int(terminal.get("embedding_cache_miss_count") or 0),
    )
    if any(terminal.get(key) != value for key, value in recomputed.items()):
        raise ValueError("v5 protected terminal statistics differ")
    if terminal.get("exact_code_sha") != final_freeze.get("protected_runner_sha"):
        raise ValueError("v5 protected terminal runner identity differs")
    return dict(terminal)


def exact_sign_flip_p_value(values: Sequence[float]) -> float:
    """Compute an exact one-sided paired sign-flip p-value by integer DP."""

    scaled = []
    for value in values:
        finite = _finite(value, "paired difference")
        doubled = round(finite * 2)
        if not math.isclose(finite * 2, doubled, abs_tol=1e-9):
            raise ValueError("v5 protected paired differences must use half-action precision")
        scaled.append(int(doubled))
    if not scaled:
        return 1.0
    distribution: Counter[int] = Counter({0: 1})
    for value in scaled:
        updated: Counter[int] = Counter()
        for total, count in distribution.items():
            updated[total + value] += count
            updated[total - value] += count
        distribution = updated
    observed = sum(scaled)
    extreme = sum(count for total, count in distribution.items() if total >= observed)
    return extreme / (1 << len(scaled))


def sign_protected_artifact(
    body: Mapping[str, Any], *, signer: ProtectedStudySigner, kind: str
) -> dict[str, Any]:
    """Sign one content-addressed protected-study artifact with KMS HMAC."""

    key_id = str(getattr(signer, "key_id", "")).strip()
    if not key_id:
        raise ValueError("v5 protected signing key identity is required")
    artifact_sha256 = sha256_hex(dict(body))
    mac = signer.token(kind=kind, raw_id=artifact_sha256)
    _require_sha256(mac, "artifact signature")
    return {
        **dict(body),
        "artifact_sha256": artifact_sha256,
        "signature": {
            "algorithm": SIGNATURE_ALGORITHM,
            "kind": kind,
            "key_id_sha256": hashlib.sha256(key_id.encode("utf-8")).hexdigest(),
            "mac": mac,
        },
    }


def verify_protected_artifact(
    artifact: Mapping[str, Any], *, signer: ProtectedStudySigner, kind: str
) -> dict[str, Any]:
    """Verify content identity, signature envelope, and KMS HMAC."""

    artifact_sha256 = str(artifact.get("artifact_sha256") or "")
    _require_sha256(artifact_sha256, "artifact")
    body = {
        key: value for key, value in artifact.items() if key not in {"artifact_sha256", "signature"}
    }
    if sha256_hex(body) != artifact_sha256:
        raise ValueError("v5 protected artifact content identity differs")
    signature = artifact.get("signature")
    key_id = str(getattr(signer, "key_id", "")).strip()
    if (
        not isinstance(signature, Mapping)
        or signature.get("algorithm") != SIGNATURE_ALGORITHM
        or signature.get("kind") != kind
        or signature.get("key_id_sha256") != hashlib.sha256(key_id.encode("utf-8")).hexdigest()
    ):
        raise ValueError("v5 protected artifact signature envelope differs")
    expected = signer.token(kind=kind, raw_id=artifact_sha256)
    if not hmac.compare_digest(str(signature.get("mac") or ""), expected):
        raise ValueError("v5 protected artifact signature is invalid")
    return dict(artifact)


def write_private_json_exclusive(
    path: str | os.PathLike[str], value: Mapping[str, Any]
) -> pathlib.Path:
    """Create one private JSON artifact once; identical rechecks are idempotent."""

    target = pathlib.Path(path).expanduser().resolve(strict=False)
    repository = pathlib.Path(__file__).resolve().parents[2]
    if target == repository or repository in target.parents:
        raise ValueError("v5 protected artifacts must remain outside the repository")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    if target.exists():
        observed = json.loads(target.read_text(encoding="utf-8"))
        if observed != dict(value):
            raise FileExistsError("v5 protected artifact already exists with different content")
        return target
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    directory_fd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return target


def persist_protected_corpus_items(
    *, corpus: Mapping[str, Any], directory: str | os.PathLike[str]
) -> dict[str, Any]:
    """Persist every constructed item independently and resume by exact identity."""

    _validate_corpus(corpus)
    root = pathlib.Path(directory).expanduser().resolve(strict=False)
    repository = pathlib.Path(__file__).resolve().parents[2]
    if root == repository or repository in root.parents:
        raise ValueError("v5 protected corpus items must remain outside the repository")
    entries = []
    for group in ("primary", "reserve"):
        for family in MECHANISM_FAMILIES:
            for scenario in corpus[group][family]:
                item_sha256 = sha256_hex(scenario)
                target = root / group / family / f"{scenario['scenario_id']}.json"
                write_private_json_exclusive(target, scenario)
                entries.append(
                    {
                        "group": group,
                        "family": family,
                        "scenario_id": scenario["scenario_id"],
                        "item_sha256": item_sha256,
                        "relative_path": str(target.relative_to(root)),
                    }
                )
    body = {
        "schema_version": 1,
        "status": "protected_corpus_items_persisted",
        "corpus_sha256": corpus["corpus_sha256"],
        "item_count": len(entries),
        "items": entries,
    }
    manifest = {**body, "manifest_sha256": sha256_hex(body)}
    write_private_json_exclusive(root / "item-manifest.json", manifest)
    return manifest


def _validate_power_plan(power_plan: Mapping[str, Any]) -> None:
    body = {key: value for key, value in power_plan.items() if key != "power_plan_sha256"}
    count = int(power_plan.get("protected_scenario_count") or 0)
    if (
        power_plan.get("status") != "power_plan_frozen"
        or power_plan.get("protocol_sha256") != protected_study_protocol()["protocol_sha256"]
        or power_plan.get("power_plan_sha256") != sha256_hex(body)
        or count < PROTECTED_MINIMUM_SCENARIOS
        or count > PROTECTED_MAXIMUM_SCENARIOS
        or count % len(MECHANISM_FAMILIES)
    ):
        raise ValueError("v5 protected power plan is not eligible")


def _validate_corpus(corpus: Mapping[str, Any]) -> None:
    body = {key: value for key, value in corpus.items() if key != "corpus_sha256"}
    if corpus.get("status") != "protected_corpus_constructed" or corpus.get(
        "corpus_sha256"
    ) != sha256_hex(body):
        raise ValueError("v5 protected corpus identity differs")
    for group in ("primary", "reserve"):
        value = corpus.get(group)
        if not isinstance(value, Mapping) or set(value) != set(MECHANISM_FAMILIES):
            raise ValueError("v5 protected corpus family balance differs")


def _validate_review_binding(*, corpus: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    _validate_corpus(corpus)
    body = {key: value for key, value in state.items() if key != "review_state_sha256"}
    if state.get("corpus_sha256") != corpus.get("corpus_sha256") or state.get(
        "review_state_sha256"
    ) != sha256_hex(body):
        raise ValueError("v5 protected review state differs from its corpus")
    previous = None
    for index, event in enumerate(state.get("events") or [], start=1):
        event_body = {key: value for key, value in event.items() if key != "event_sha256"}
        if (
            event.get("sequence") != index
            or event.get("previous_event_sha256") != previous
            or event.get("event_sha256") != sha256_hex(event_body)
        ):
            raise ValueError("v5 protected review event chain differs")
        previous = event["event_sha256"]


def _scenario_by_id(corpus: Mapping[str, Any], scenario_id: str) -> dict[str, Any]:
    for group in ("primary", "reserve"):
        for family in MECHANISM_FAMILIES:
            for scenario in corpus[group][family]:
                if scenario["scenario_id"] == scenario_id:
                    return dict(scenario)
    raise ValueError("v5 protected review scenario is absent")


def _applicable_target(scenario: Mapping[str, Any]) -> dict[str, Any]:
    recurrence = scenario["agent_view"]["recurrence"]
    candidates = [
        memory
        for memory in scenario["agent_view"]["memories"]
        if memory.get("status") == "active"
        and memory.get("operator_disposition") == "approved"
        and memory.get("safety_status") == "safe"
        and memory.get("contradiction_status") == "supported"
        and memory.get("usage_instruction") == "positive_guidance"
        and applicability_matches(
            memory["applicability"],
            service=recurrence["service"],
            workload=recurrence["workload"],
            observations=recurrence["initial_observation"],
        )
    ]
    if len(candidates) != 1:
        raise ValueError("v5 protected scenario has no unique applicable target")
    return dict(candidates[0])


def _timestamp(value: Any, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"v5 protected {label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"v5 protected {label} timestamp requires a timezone")
    return parsed.astimezone(UTC)


def _finite(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"v5 protected {label} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"v5 protected {label} must be finite")
    return result


def _require_sha256(value: str, label: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"v5 protected {label} identity must be SHA-256")


def _require_code_sha(value: str, label: str) -> None:
    if not _CODE_SHA_RE.fullmatch(value):
        raise ValueError(f"v5 protected {label} identity must be an exact commit SHA")


__all__ = [
    "CORPUS_SEAL_KIND",
    "ACTION_RESPONSE_SCHEMA",
    "ACTION_SYSTEM_PROMPT",
    "EMBEDDING_CHECKPOINT_KIND",
    "FINAL_FREEZE_KIND",
    "PILOT_RESULT_KIND",
    "PILOT_ATTEMPT_AUTHORIZATION_KIND",
    "PROTECTED_ARMS",
    "PROTECTED_PROTOCOL_V2_SHA256",
    "PROTECTED_REASONING_TIMEOUT_SECONDS",
    "PROTECTED_REPETITIONS",
    "RETRIEVAL_RESULT_KIND",
    "TERMINAL_RESULT_KIND",
    "build_final_freeze",
    "build_behavioral_pilot_result",
    "build_pilot_attempt_authorization",
    "derive_protected_corpus",
    "deterministic_arm_order",
    "evaluate_terminal_result",
    "exact_sign_flip_p_value",
    "new_review_state",
    "next_review_item",
    "owner_review_guide",
    "paired_differences_from_trials",
    "persist_protected_corpus_items",
    "power_plan_from_pilot",
    "protected_study_protocol",
    "record_review_event",
    "reference_lesson",
    "seal_reviewed_corpus",
    "sign_protected_artifact",
    "validate_beacon_receipt",
    "validate_pilot_attempt_authorization",
    "validate_terminal_artifact",
    "verify_protected_artifact",
    "write_private_json_exclusive",
]
