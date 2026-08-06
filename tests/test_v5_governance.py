from __future__ import annotations

import copy
import hashlib
import random
from typing import Any

import pytest

from hindsight import v5_governance as governance
from hindsight import v5_qualification as qualification
from hindsight.v5_corpus import sha256_hex


SUBJECT_SHA = "7" * 40
EVALUATOR_SHA = "8" * 40
DIAGNOSTIC_FILE_SHA256 = "9" * 64
TARGET_ID = f"v5m-{'1' * 24}"


class FakeSigner:
    key_id = "arn:aws:kms:us-east-1:111122223333:key/v5-governance-test"

    def __init__(self, secret: str = "v5-governance-test-secret") -> None:
        self.secret = secret

    def token(self, *, kind: str, raw_id: str) -> str:
        return hashlib.sha256(f"{self.secret}\0{kind}\0{raw_id}".encode()).hexdigest()


class SummaryCheckpoint:
    delegate_identity = {
        "provider": "gemini",
        "model": "gemini-embedding-2",
        "dimensions": 1024,
        "capability": "semantic",
        "encoder_revision": "gemini-retrieval-task-v1",
        "representation": "raw_control",
    }
    checkpoint_sha256 = "b" * 64
    attestation_key_id_sha256 = "c" * 64

    @property
    def entry_counts(self) -> dict[str, int]:
        return {
            qualification.DOCUMENT_TASK: qualification.EXPECTED_UNIQUE_DOCUMENTS,
            qualification.QUERY_TASK: qualification.EXPECTED_SCENARIO_COUNT,
        }

    @property
    def delegate_call_counts(self) -> dict[str, int]:
        return {
            qualification.DOCUMENT_TASK: qualification.EXPECTED_UNIQUE_DOCUMENTS,
            qualification.QUERY_TASK: qualification.EXPECTED_SCENARIO_COUNT,
        }

    @property
    def cache_hit_counts(self) -> dict[str, int]:
        return {qualification.DOCUMENT_TASK: 0, qualification.QUERY_TASK: 0}


def _scenario_ids() -> list[str]:
    return [f"v5s-{index:024x}" for index in range(600)]


def _row(index: int) -> dict[str, Any]:
    rank_failed = index >= 574
    old_parity_failed = 521 <= index <= 568 or 574 <= index <= 578
    row = {
        "scenario_id": _scenario_ids()[index],
        "status": "qualified",
        "candidate_count": 4,
        "policy": "semantic_strict",
        "fallback_reason": None,
        "retrieval_id": f"00000000-0000-0000-0000-{index + 1:012x}",
        "direct_candidate_ids": [TARGET_ID],
        "indexed_candidate_ids": [TARGET_ID],
        "intrinsic_match_count": 1,
        "matching_rank": 2 if rank_failed else 1,
        "indexed_matching_rank": 2 if rank_failed else 1,
        "rank_one_distance": 0.2,
        "rank_one_margin": -0.01 if rank_failed else 0.01,
        "ineligible_candidate_absent": True,
        "ineligible_read_absent": True,
        "audit_only_visible": True,
        "membership_parity": True,
        "order_parity": True,
        "index_parity": not old_parity_failed,
        "max_distance_delta": 1.7440744337626768e-6 if old_parity_failed else 5e-7,
        "alternate_tenant_visible": False,
        "alternate_retrieval_visible": False,
        "alternate_current_semantic_visible": False,
        "alternate_audit_visible": False,
        "alternate_learning_reads_visible": False,
        "learning_decision_sealed": True,
        "alternate_decision_sealed": True,
    }
    if rank_failed or old_parity_failed:
        row["status"] = "failed"
    return row


def _diagnostic() -> dict[str, Any]:
    rows = [_row(index) for index in range(600)]
    body = {
        "schema_version": 2,
        "status": "diagnostic_only",
        "qualification_claim": False,
        "code_sha": SUBJECT_SHA,
        "qualification_contract_sha256": governance.V1_QUALIFICATION_CONTRACT_SHA256,
        "structural_receipt_sha256": "a" * 64,
        "execution_manifest_sha256": "e" * 64,
        "database_name": "hindsight_v5_development_unit",
        "database_engine": "cockroachdb",
        "database_engine_version_sha256": "1" * 64,
        "database_build_version_sha256": "2" * 64,
        "database_build_description_sha256": "3" * 64,
        "database_cluster_id_sha256": "4" * 64,
        "deploy_database_identity_sha256": "5" * 64,
        "runtime_database_identity_sha256": "6" * 64,
        "provider_identity": {
            "provider": "gemini",
            "model": "gemini-embedding-2",
            "dimensions": 1024,
            "capability": "semantic",
            "encoder_revision": "gemini-retrieval-task-v1",
            "representation": "raw_control",
        },
        "checkpoint_sha256": "b" * 64,
        "checkpoint_attestation_key_id_sha256": "c" * 64,
        "checkpoint_entry_counts": {
            qualification.DOCUMENT_TASK: qualification.EXPECTED_UNIQUE_DOCUMENTS,
            qualification.QUERY_TASK: qualification.EXPECTED_SCENARIO_COUNT,
        },
        "delegate_call_counts": {
            qualification.DOCUMENT_TASK: 400,
            qualification.QUERY_TASK: 100,
        },
        "cache_hit_counts": {
            qualification.DOCUMENT_TASK: 4400,
            qualification.QUERY_TASK: 2300,
        },
        "scenario_count": 600,
        "qualified_row_count": sum(row["status"] == "qualified" for row in rows),
        "results": rows,
    }
    return {**body, "diagnostic_sha256": sha256_hex(body)}


def _patch_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    selected = [{"scenario_id": scenario_id} for scenario_id in _scenario_ids()]
    monkeypatch.setattr(governance, "select_embedding_scenarios", lambda *, code_sha: selected)
    monkeypatch.setattr(
        qualification,
        "select_embedding_scenarios",
        lambda *, code_sha: selected,
    )


def _evaluate(
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: dict[str, Any],
    signer: FakeSigner | None = None,
) -> dict[str, Any]:
    _patch_selection(monkeypatch)
    return governance.evaluate_governance_v2(
        diagnostic=diagnostic,
        expected_diagnostic_sha256=diagnostic["diagnostic_sha256"],
        diagnostic_file_sha256=DIAGNOSTIC_FILE_SHA256,
        tested_subject_sha=SUBJECT_SHA,
        policy_evaluator_sha=EVALUATOR_SHA,
        signer=signer or FakeSigner(),
    )


def _normalize_diagnostic(diagnostic: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(diagnostic)
    rows = value["results"]
    for row in rows:
        row["status"] = "qualified" if governance._v1_row_qualified(row) else "failed"
    value["qualified_row_count"] = sum(row["status"] == "qualified" for row in rows)
    body = {key: item for key, item in value.items() if key != "diagnostic_sha256"}
    value["diagnostic_sha256"] = sha256_hex(body)
    return value


def test_v1_rejects_while_v2_accepts_the_complete_574_of_600_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = _diagnostic()
    authorization = _evaluate(monkeypatch, diagnostic)

    assert diagnostic["qualified_row_count"] == 526
    assert authorization["status"] == "v2_passed"
    assert authorization["semantic_rank_one_count"] == 574
    assert authorization["semantic_rank_one_accuracy_basis_points"] == 9567
    assert authorization["semantic_rank_one_accuracy_display"] == "95.7%"
    assert authorization["maximum_distance_delta"] == pytest.approx(1.7440744337626768e-6)
    assert authorization["individual_scenario_exclusions"] == 0
    policy_artifact = governance.governance_v2_policy_artifact(
        tested_subject_sha=SUBJECT_SHA,
        policy_evaluator_sha=EVALUATOR_SHA,
    )
    assert policy_artifact["tested_subject_sha"] == SUBJECT_SHA
    assert policy_artifact["policy_evaluator_sha"] == EVALUATOR_SHA
    assert authorization["policy_artifact_sha256"] == policy_artifact["policy_artifact_sha256"]

    structural_body = {
        "status": "qualified",
        "code_sha": SUBJECT_SHA,
        "protocol_sha256": qualification.development_protocol()["protocol_sha256"],
        "scenario_count": 6_000,
        "corpus_sha256": "f" * 64,
    }
    structural = {**structural_body, "receipt_sha256": sha256_hex(structural_body)}
    with pytest.raises(ValueError, match="non-qualified scenario"):
        qualification.summarize_qualification_results(
            code_sha=SUBJECT_SHA,
            database_name="hindsight_v5_development_unit",
            results=diagnostic["results"],
            checkpoint=SummaryCheckpoint(),  # type: ignore[arg-type]
            structural_receipt=structural,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ineligible_candidate_absent", False),
        ("ineligible_read_absent", False),
        ("audit_only_visible", False),
        ("alternate_tenant_visible", True),
        ("learning_decision_sealed", False),
        ("alternate_decision_sealed", False),
        ("policy", "semantic_fallback"),
        ("fallback_reason", "provider_unavailable"),
        ("membership_parity", False),
        ("order_parity", False),
    ],
)
def test_v2_rejects_every_safety_and_retrieval_hard_gate_failure(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    diagnostic = _diagnostic()
    diagnostic["results"][-1][field] = value
    diagnostic = _normalize_diagnostic(diagnostic)
    with pytest.raises(ValueError, match="does not satisfy governance V2"):
        _evaluate(monkeypatch, diagnostic)


def test_v2_rejects_semantic_accuracy_below_ninety_percent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = _diagnostic()
    for row in diagnostic["results"][539:574]:
        row["matching_rank"] = 2
        row["indexed_matching_rank"] = 2
        row["rank_one_margin"] = -0.01
    diagnostic = _normalize_diagnostic(diagnostic)
    with pytest.raises(ValueError, match="does not satisfy governance V2"):
        _evaluate(monkeypatch, diagnostic)


def test_v2_rejects_excessive_distance_delta_and_altered_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = _diagnostic()
    diagnostic["results"][-1]["max_distance_delta"] = 2.0000001e-6
    diagnostic = _normalize_diagnostic(diagnostic)
    with pytest.raises(ValueError, match="does not satisfy governance V2"):
        _evaluate(monkeypatch, diagnostic)

    altered = _diagnostic()
    expected = altered["diagnostic_sha256"]
    altered["results"][-1]["rank_one_distance"] = 0.19
    body = {key: value for key, value in altered.items() if key != "diagnostic_sha256"}
    altered["diagnostic_sha256"] = sha256_hex(body)
    _patch_selection(monkeypatch)
    with pytest.raises(ValueError, match="content identity differs"):
        governance.evaluate_governance_v2(
            diagnostic=altered,
            expected_diagnostic_sha256=expected,
            diagnostic_file_sha256=DIAGNOSTIC_FILE_SHA256,
            tested_subject_sha=SUBJECT_SHA,
            policy_evaluator_sha=EVALUATOR_SHA,
            signer=FakeSigner(),
        )


def test_v2_verification_rejects_invalid_signatures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = FakeSigner()
    diagnostic = _diagnostic()
    authorization = _evaluate(monkeypatch, diagnostic, signer)
    authorization["signature"]["mac"] = "0" * 64
    with pytest.raises(ValueError, match="signature is invalid"):
        governance.verify_governance_v2(
            authorization=authorization,
            diagnostic=diagnostic,
            expected_diagnostic_sha256=diagnostic["diagnostic_sha256"],
            diagnostic_file_sha256=DIAGNOSTIC_FILE_SHA256,
            tested_subject_sha=SUBJECT_SHA,
            policy_evaluator_sha=EVALUATOR_SHA,
            signer=signer,
        )


def test_binary32_precision_characterization_supports_two_micro_tolerance() -> None:
    generator = random.Random(2684)
    query = [generator.uniform(-1.0, 1.0) for _ in range(1024)]
    document = [0.7 * value + 0.3 * generator.uniform(-1.0, 1.0) for value in query]
    direct = governance._cosine_distance(query, document)
    stored = governance.binary32_cosine_distance(query, document)
    delta = abs(direct - stored)

    assert 1e-6 < delta <= governance.V2_MAXIMUM_DISTANCE_DELTA


def test_cache_only_provider_fails_closed_on_any_missing_embedding() -> None:
    provider = governance.CacheOnlyEmbeddingProvider()
    with pytest.raises(RuntimeError, match="document embedding miss"):
        provider.embed_document("missing document")
    with pytest.raises(RuntimeError, match="query embedding miss"):
        provider.embed_query("missing query")
    assert provider.miss_count == 2


def test_protected_authorization_requires_passing_cache_only_rehearsals_and_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = FakeSigner()
    diagnostic = _diagnostic()
    authorization = _evaluate(monkeypatch, diagnostic, signer)
    rehearsal_body = {
        "schema_version": governance.REHEARSAL_SCHEMA_VERSION,
        "status": "rehearsals_passed",
        "policy_revision": governance.POLICY_REVISION,
        "policy_sha256": governance.governance_v2_policy()["policy_sha256"],
        "tested_subject_sha": SUBJECT_SHA,
        "policy_evaluator_sha": EVALUATOR_SHA,
        "source_authorization_sha256": authorization["artifact_sha256"],
        "source_diagnostic_sha256": diagnostic["diagnostic_sha256"],
        "source_diagnostic_file_sha256": DIAGNOSTIC_FILE_SHA256,
        "database_name": "hindsight_v5_development_rehearsal",
        "database_engine": "cockroachdb",
        "rehearsal_selection_sha256": (governance.EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256),
        "scenario_count": 60,
        "semantic_rank_one_count": 55,
        "semantic_rank_one_accuracy_basis_points": 9167,
        "maximum_distance_delta": 1.2e-6,
        "all_hard_gates_passed": True,
        "checkpoint_sha256": "b" * 64,
        "checkpoint_entry_counts": {
            qualification.DOCUMENT_TASK: qualification.EXPECTED_UNIQUE_DOCUMENTS,
            qualification.QUERY_TASK: qualification.EXPECTED_SCENARIO_COUNT,
        },
        "embedding_cache_miss_count": 0,
        "embedding_delegate_call_counts": {
            qualification.DOCUMENT_TASK: 0,
            qualification.QUERY_TASK: 0,
        },
        "monitoring": {
            "phase": "development_rehearsal",
            "progress_records": 60,
            "fail_closed": True,
        },
        "audit": {
            "retrieval_decisions_sealed": True,
            "alternate_decisions_sealed": True,
            "result_sha256": "d" * 64,
        },
    }
    rehearsal = governance._sign_artifact(
        rehearsal_body,
        signer=signer,
        kind=governance.V2_REHEARSAL_KIND,
    )
    protected = governance.authorize_protected_learning(
        authorization=authorization,
        rehearsal_result=rehearsal,
        diagnostic=diagnostic,
        expected_diagnostic_sha256=diagnostic["diagnostic_sha256"],
        diagnostic_file_sha256=DIAGNOSTIC_FILE_SHA256,
        tested_subject_sha=SUBJECT_SHA,
        policy_evaluator_sha=EVALUATOR_SHA,
        signer=signer,
    )

    assert protected["status"] == "protected_learning_enabled"
    assert (
        governance.verify_protected_learning_authorization(
            protected_authorization=protected,
            signer=signer,
        )
        == protected
    )
    assert protected["controls"] == governance.protected_learning_controls()

    missed_body = {**rehearsal_body, "embedding_cache_miss_count": 1}
    missed = governance._sign_artifact(
        missed_body,
        signer=signer,
        kind=governance.V2_REHEARSAL_KIND,
    )
    with pytest.raises(ValueError, match="cache-only governance V2"):
        governance.authorize_protected_learning(
            authorization=authorization,
            rehearsal_result=missed,
            diagnostic=diagnostic,
            expected_diagnostic_sha256=diagnostic["diagnostic_sha256"],
            diagnostic_file_sha256=DIAGNOSTIC_FILE_SHA256,
            tested_subject_sha=SUBJECT_SHA,
            policy_evaluator_sha=EVALUATOR_SHA,
            signer=signer,
        )
