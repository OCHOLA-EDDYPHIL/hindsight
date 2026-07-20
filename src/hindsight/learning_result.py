"""Classify a frozen learning execution without turning science into CI status."""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

from hindsight.benchmark import benchmark_report, finalize_interrupted_experiments
from hindsight.db import connect
from hindsight.learning_authority import (
    execution_authorization_id,
    protocol_authorization_id,
    require_execution_lease,
)
from hindsight.server_tenants import learning_tenant_id
from hindsight.tenant import tenant_scope

_PROTOCOL_GATES = {
    "confirmation_only",
    "complete_pairs",
    "independent_sample_size",
    "retrieval",
    "identity_lineage",
    "preparation",
    "target_bindings",
    "context_parity",
    "preregistration",
    "no_prior_scientific_attempt",
    "binding_history",
}


def finalize_and_classify(
    *,
    db_url: str,
    sequence: int,
    workflow_run_id: int,
    workflow_run_attempt: int,
    code_sha: str,
    protocol_authorization_sha256: str,
    interruption_reason: str,
) -> dict[str, Any]:
    """Fence interrupted rows, then return one canonical terminal study report."""

    lease = require_execution_lease(
        db_url=db_url,
        sequence=sequence,
        workflow_run_id=workflow_run_id,
        workflow_run_attempt=workflow_run_attempt,
        code_sha=code_sha,
        protocol_authorization_sha256=protocol_authorization_sha256,
    )
    with tenant_scope(learning_tenant_id()):
        finalized = finalize_interrupted_experiments(
            code_sha=code_sha,
            reason=interruption_reason,
            db_url=db_url,
        )
        experiments = _experiments(db_url=db_url)
        classification = _classify(experiments=experiments, db_url=db_url)
    return {
        "schema_version": 1,
        "sequence": sequence,
        "protocol_authorization_id": protocol_authorization_id(),
        "protocol_authorization_sha256": protocol_authorization_sha256,
        "protocol_identity_sha256": str(lease["protocol_identity_sha256"]),
        "execution_authorization_id": execution_authorization_id(sequence),
        "workflow_run_id": workflow_run_id,
        "workflow_run_attempt": workflow_run_attempt,
        "code_sha": code_sha,
        "result": classification["result"],
        "protocol_valid": classification["protocol_valid"],
        "reason_code": classification["reason_code"],
        "terminal_class": classification["terminal_class"],
        "terminal_reason": classification["terminal_reason"],
        "confirmation_report": classification.get("confirmation_report"),
        "execution_summary": {
            "experiment_count": len(experiments),
            "experiment_statuses": [
                {
                    "experiment_kind": row["experiment_kind"],
                    "status": row["status"],
                    "outcome_bearing": bool(row["outcome_bearing"]),
                    "scientific_failure": bool(row["scientific_failure"]),
                }
                for row in experiments
            ],
            "interrupted_experiments_finalized": int(finalized["experiments"]),
        },
    }


def _experiments(*, db_url: str) -> list[dict[str, Any]]:
    with connect(db_url, application_name="hindsight-learning-result") as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                    SELECT experiment.id, experiment.experiment_kind,
                        experiment.status, experiment.created_at,
                        EXISTS (
                            SELECT 1 FROM benchmark_variant_preparations AS preparation
                            WHERE preparation.experiment_id = experiment.id
                                AND preparation.status = 'scientific_failed'
                        ) AS scientific_failure,
                        EXISTS (
                            SELECT 1 FROM benchmark_trials AS trial
                            WHERE trial.experiment_id = experiment.id
                                AND (
                                    trial.status IN ('completed', 'invalid')
                                    OR trial.penalized_action_count IS NOT NULL
                                    OR EXISTS (
                                        SELECT 1 FROM benchmark_actions AS action
                                        WHERE action.trial_id = trial.id
                                    )
                                )
                        ) AS outcome_bearing
                    FROM benchmark_experiments AS experiment
                    WHERE experiment.protocol_authorization_id = %s
                      AND experiment.experiment_kind IN ('pilot', 'confirmation')
                    ORDER BY experiment.created_at, experiment.id
                """,
                (protocol_authorization_id(),),
            )
            return [dict(row) for row in cur.fetchall()]


def _classify(*, experiments: list[dict[str, Any]], db_url: str) -> dict[str, Any]:
    confirmations = [
        row for row in experiments if row["experiment_kind"] == "confirmation"
    ]
    scientific_failures = [row for row in experiments if row["scientific_failure"]]
    if scientific_failures:
        return _terminal(
            result="not_demonstrated",
            protocol_valid=True,
            reason_code="scientific_preparation_failure",
            terminal_class="scientific_terminal",
            terminal_reason="A frozen scientific preparation gate failed.",
        )
    if not confirmations:
        return _terminal(
            result="inconclusive",
            protocol_valid=True,
            reason_code="infrastructure_before_confirmation",
            terminal_class="infrastructure_outcome_free",
            terminal_reason="Infrastructure ended before any confirmation outcome existed.",
        )
    confirmation = confirmations[-1]
    if confirmation["status"] != "completed":
        outcome_bearing = bool(confirmation["outcome_bearing"])
        return _terminal(
            result="inconclusive",
            protocol_valid=True,
            reason_code=(
                "infrastructure_after_confirmation_outcome"
                if outcome_bearing
                else "infrastructure_before_confirmation_outcome"
            ),
            terminal_class=(
                "infrastructure_outcome_bearing"
                if outcome_bearing
                else "infrastructure_outcome_free"
            ),
            terminal_reason=(
                "Infrastructure ended after confirmation outcomes existed."
                if outcome_bearing
                else "Infrastructure ended before any confirmation outcome existed."
            ),
        )
    report = benchmark_report(experiment_id=str(confirmation["id"]), db_url=db_url)
    gates = dict(report.get("gates") or {})
    protocol_valid = bool(_PROTOCOL_GATES) and all(
        gates.get(gate) is True for gate in _PROTOCOL_GATES
    )
    if not protocol_valid:
        return _terminal(
            result="inconclusive",
            protocol_valid=False,
            reason_code="protocol_gate_failed",
            terminal_class="protocol_terminal",
            terminal_reason="A frozen protocol-integrity gate did not pass.",
            confirmation_report=report,
        )
    if report.get("claim_authorized") is True and all(
        value is True for value in gates.values()
    ):
        return _terminal(
            result="accepted",
            protocol_valid=True,
            reason_code="all_preregistered_gates_passed",
            terminal_class="claim_authorized",
            terminal_reason="Every preregistered scientific and safety gate passed.",
            confirmation_report=report,
        )
    return _terminal(
        result="not_demonstrated",
        protocol_valid=True,
        reason_code="scientific_or_safety_gate_not_met",
        terminal_class="not_demonstrated",
        terminal_reason="The valid study did not pass every scientific and safety gate.",
        confirmation_report=report,
    )


def _terminal(
    *,
    result: str,
    protocol_valid: bool,
    reason_code: str,
    terminal_class: str,
    terminal_reason: str,
    confirmation_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "result": result,
        "protocol_valid": protocol_valid,
        "reason_code": reason_code,
        "terminal_class": terminal_class,
        "terminal_reason": terminal_reason,
        "confirmation_report": confirmation_report,
    }
