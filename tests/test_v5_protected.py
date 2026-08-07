from __future__ import annotations

import copy
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from hindsight import v5_protected
from hindsight import v5_protected_execution
from hindsight.reasoning import ReasoningResponse
from hindsight.v5_corpus import MECHANISM_FAMILIES, compile_scenario
from hindsight.v5_protected_runtime import (
    BehavioralStudyRunner,
    MonitorLease,
    ProtectedAuditStore,
    ProtectedEvidenceArchive,
    ProtectedRunFailure,
)


SUBJECT_SHA = "717adc5d443abb716499c54d3718c030cb604ceb"
EVALUATOR_SHA = "564607ec13b40a9b831d7ea4ec44f32991265192"
RUNNER_SHA = "a" * 40
AUTH_SHA = "b" * 64
PILOT_SHA = "c" * 64


class _Signer:
    key_id = "alias/test-v5-protected"

    def token(self, *, kind: str, raw_id: str) -> str:
        return v5_protected.sha256_hex([kind, raw_id, self.key_id])


class _Embedding:
    provider_name = "gemini"
    model_name = "gemini-embedding-2"
    dimensions = 1024
    capability = "semantic"
    encoder_revision = "gemini-retrieval-task-v1"
    representation = "raw_control"

    def embed_document(self, text: str) -> list[float]:
        return self._vector(text)

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        result = [0.0] * 1024
        result[int(v5_protected.sha256_hex(text)[:4], 16) % 1024] = 1.0
        return result


class _CacheMissCounter:
    miss_count = 0


class _DevelopmentDocuments:
    def __init__(self, counter: _CacheMissCounter) -> None:
        self.counter = counter

    def embed_document(self, text: str) -> list[float]:
        if text == "cached":
            return [1.0] + [0.0] * 1023
        self.counter.miss_count += 1
        raise RuntimeError("cache miss")


def _power_plan() -> dict:
    return v5_protected.power_plan_from_pilot(
        efficacy_differences=[1.0] * 60,
        reference_differences=[0.0] * 60,
    )


def _freeze() -> dict:
    return v5_protected.build_final_freeze(
        tested_subject_sha=SUBJECT_SHA,
        policy_evaluator_sha=EVALUATOR_SHA,
        protected_runner_sha=RUNNER_SHA,
        source_protected_authorization_sha256=AUTH_SHA,
        source_pilot_sha256=PILOT_SHA,
        power_plan=_power_plan(),
        recorded_at="2026-08-07T00:00:00+00:00",
        signer=_Signer(),
    )


def _beacon() -> dict:
    return {
        "source": "nist-randomness-beacon-v2",
        "pulse_uri": "https://beacon.nist.gov/beacon/2.0/pulse/time/1786061460",
        "previous_pulse_uri": "https://beacon.nist.gov/beacon/2.0/pulse/time/1786061400",
        "output_value": "d" * 64,
        "signature_value": "e" * 128,
        "published_at": "2026-08-07T00:11:00+00:00",
        "previous_published_at": "2026-08-07T00:09:00+00:00",
        "raw_response_sha256": "f" * 64,
        "previous_raw_response_sha256": "1" * 64,
        "signature_verification": {
            "verified": True,
            "method": "nist-beacon-v2-reference-verifier",
            "verifier_sha256": "2" * 64,
            "certificate_sha256": "3" * 64,
        },
    }


def _corpus(monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setattr(v5_protected, "development_scenarios", lambda **_kwargs: [])
    return v5_protected.derive_protected_corpus(
        final_freeze=_freeze(),
        beacon=_beacon(),
    )


def test_protocol_power_and_signed_freeze_are_deterministic() -> None:
    protocol = v5_protected.protected_study_protocol()
    assert protocol["arms"]["reference_lesson"] == "direct-oracle-derived-positive-control"
    assert protocol["execution"]["embedding_mode"] == "cache-only"
    assert protocol == v5_protected.protected_study_protocol()

    power = _power_plan()
    assert power["status"] == "power_plan_frozen"
    assert power["protected_scenario_count"] == 36
    assert power["scenarios_per_family"] == 6
    assert power["reserve_per_family"] == 2

    frozen = _freeze()
    assert frozen["status"] == "study_frozen"
    assert frozen["tested_subject_sha"] == SUBJECT_SHA
    assert v5_protected.verify_protected_artifact(
        frozen,
        signer=_Signer(),
        kind=v5_protected.FINAL_FREEZE_KIND,
    ) == frozen

    altered = copy.deepcopy(frozen)
    altered["protected_runner_sha"] = "2" * 40
    with pytest.raises(ValueError, match="content identity"):
        v5_protected.verify_protected_artifact(
            altered,
            signer=_Signer(),
            kind=v5_protected.FINAL_FREEZE_KIND,
        )


def test_power_plan_fails_closed_above_the_maximum() -> None:
    values = [float(index * 10) for index in range(60)]
    result = v5_protected.power_plan_from_pilot(
        efficacy_differences=values,
        reference_differences=list(reversed(values)),
    )
    assert result["status"] == "power_plan_infeasible"
    assert result["protected_scenario_count"] > 120
    with pytest.raises(ValueError, match="power plan is not eligible"):
        v5_protected.build_final_freeze(
            tested_subject_sha=SUBJECT_SHA,
            policy_evaluator_sha=EVALUATOR_SHA,
            protected_runner_sha=RUNNER_SHA,
            source_protected_authorization_sha256=AUTH_SHA,
            source_pilot_sha256=PILOT_SHA,
            power_plan=result,
            recorded_at="2026-08-07T00:00:00+00:00",
            signer=_Signer(),
        )


def test_beacon_requires_the_first_pulse_after_freeze_boundary() -> None:
    freeze_at = datetime(2026, 8, 7, tzinfo=UTC)
    assert v5_protected.validate_beacon_receipt(
        _beacon(),
        freeze_recorded_at=freeze_at,
    )["output_value"] == "d" * 64

    late_previous = {**_beacon(), "previous_published_at": "2026-08-07T00:10:30+00:00"}
    with pytest.raises(ValueError, match="first pulse"):
        v5_protected.validate_beacon_receipt(
            late_previous,
            freeze_recorded_at=freeze_at,
        )

    early = {**_beacon(), "published_at": (freeze_at + timedelta(minutes=9)).isoformat()}
    with pytest.raises(ValueError, match="first pulse"):
        v5_protected.validate_beacon_receipt(early, freeze_recorded_at=freeze_at)


def test_protected_corpus_is_balanced_separate_and_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _corpus(monkeypatch)
    assert corpus == _corpus(monkeypatch)
    assert corpus["primary_count"] == 36
    assert corpus["reserve_count"] == 12
    assert all(len(corpus["primary"][family]) == 6 for family in MECHANISM_FAMILIES)
    assert all(len(corpus["reserve"][family]) == 2 for family in MECHANISM_FAMILIES)
    identities = [
        row["scenario_id"]
        for group in ("primary", "reserve")
        for family in MECHANISM_FAMILIES
        for row in corpus[group][family]
    ]
    assert len(identities) == len(set(identities)) == 48


def test_protected_corpus_items_resume_without_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    corpus = _corpus(monkeypatch)
    directory = tmp_path / "items"
    first = v5_protected.persist_protected_corpus_items(
        corpus=corpus,
        directory=directory,
    )
    second = v5_protected.persist_protected_corpus_items(
        corpus=corpus,
        directory=directory,
    )
    assert first == second
    assert first["item_count"] == 48
    assert len(list(directory.glob("*/*/*.json"))) == 48


def test_owner_review_is_append_only_and_promotes_same_family_reserves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = _corpus(monkeypatch)
    state = v5_protected.new_review_state(corpus=corpus, owner="owner")
    first = v5_protected.next_review_item(corpus=corpus, state=state)
    assert first is not None
    original = first["scenario_id"]
    state = v5_protected.record_review_event(
        corpus=corpus,
        state=state,
        action="clarify",
        scenario_id=original,
        recorded_at="2026-08-07T01:00:00+00:00",
    )
    assert v5_protected.next_review_item(corpus=corpus, state=state)["scenario_id"] == original
    state = v5_protected.record_review_event(
        corpus=corpus,
        state=state,
        action="reject",
        scenario_id=original,
        recorded_at="2026-08-07T01:01:00+00:00",
    )
    replacement = v5_protected.next_review_item(corpus=corpus, state=state)
    assert replacement is not None
    assert replacement["scenario_id"] != original
    assert replacement["scenario_id"] in {
        row["scenario_id"] for row in corpus["reserve"][MECHANISM_FAMILIES[0]]
    }

    sequence = 3
    while (item := v5_protected.next_review_item(corpus=corpus, state=state)) is not None:
        state = v5_protected.record_review_event(
            corpus=corpus,
            state=state,
            action="approve",
            scenario_id=item["scenario_id"],
            recorded_at=f"2026-08-07T02:{sequence % 60:02d}:00+00:00",
        )
        sequence += 1
    assert state["status"] == "review_complete"
    sealed = v5_protected.seal_reviewed_corpus(
        corpus=corpus,
        state=state,
        signer=_Signer(),
    )
    assert sealed["status"] == "protected_corpus_sealed"
    assert sealed["selected_scenario_count"] == 36
    assert original not in sealed["selected_scenario_ids"]

    tampered = copy.deepcopy(state)
    tampered["events"][0]["action"] = "approve"
    with pytest.raises(ValueError, match="review state"):
        v5_protected.next_review_item(corpus=corpus, state=tampered)


def test_reference_control_and_arm_order_are_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    corpus = _corpus(monkeypatch)
    scenario = corpus["primary"][MECHANISM_FAMILIES[0]][0]
    reference = v5_protected.reference_lesson(scenario)
    assert reference["kind"] == "direct_reference_control"
    assert reference["diagnostic_action"] == scenario["oracle"]["unique_optimal_actions"][0]
    first = v5_protected.deterministic_arm_order(
        final_freeze_sha256=_freeze()["artifact_sha256"],
        scenario_id=scenario["scenario_id"],
        repetition=1,
    )
    assert set(first) == set(v5_protected.PROTECTED_ARMS)
    assert first == v5_protected.deterministic_arm_order(
        final_freeze_sha256=_freeze()["artifact_sha256"],
        scenario_id=scenario["scenario_id"],
        repetition=1,
    )


def test_exact_sign_flip_and_terminal_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    assert v5_protected.exact_sign_flip_p_value([1.0] * 6) == 1 / 64
    assert v5_protected.exact_sign_flip_p_value([0.0] * 120) == 1.0
    with pytest.raises(ValueError, match="half-action precision"):
        v5_protected.exact_sign_flip_p_value([0.25])

    corpus = _corpus(monkeypatch)
    state = v5_protected.new_review_state(corpus=corpus, owner="owner")
    index = 0
    while (item := v5_protected.next_review_item(corpus=corpus, state=state)) is not None:
        state = v5_protected.record_review_event(
            corpus=corpus,
            state=state,
            action="approve",
            scenario_id=item["scenario_id"],
            recorded_at=f"2026-08-07T03:{index % 60:02d}:00+00:00",
        )
        index += 1
    sealed = v5_protected.seal_reviewed_corpus(
        corpus=corpus,
        state=state,
        signer=_Signer(),
    )
    trials = []
    scores = {"no_lesson": 3.0, "reference_lesson": 2.0, "consolidated_lesson": 2.0}
    for scenario_id in sealed["selected_scenario_ids"]:
        for repetition in (1, 2):
            for arm, score in scores.items():
                trials.append(
                    {
                        "scenario_id": scenario_id,
                        "repetition": repetition,
                        "arm": arm,
                        "penalized_action_count": score,
                    }
                )
    result = v5_protected.evaluate_terminal_result(
        final_freeze=_freeze(),
        sealed_corpus=sealed,
        trials=trials,
        hard_gates_passed=True,
        rollback_state="disarmed",
        embedding_cache_miss_count=0,
    )
    assert result["status"] == "scientific_passed"
    assert result["claim_authorized"] is True
    assert result["mean_efficacy_actions"] == 1.0
    terminal = v5_protected.sign_protected_artifact(
        {
            **result,
            "exact_code_sha": RUNNER_SHA,
            "trials": trials,
            "trials_sha256": v5_protected.sha256_hex(trials),
        },
        signer=_Signer(),
        kind=v5_protected.TERMINAL_RESULT_KIND,
    )
    assert v5_protected.validate_terminal_artifact(
        terminal=terminal,
        final_freeze=_freeze(),
        sealed_corpus=sealed,
    ) == terminal

    rejected = v5_protected.evaluate_terminal_result(
        final_freeze=_freeze(),
        sealed_corpus=sealed,
        trials=trials,
        hard_gates_passed=False,
        rollback_state="executed",
        embedding_cache_miss_count=1,
    )
    assert rejected["status"] == "scientific_failed"
    assert rejected["claim_authorized"] is False


def test_private_artifacts_are_no_clobber(tmp_path: Path) -> None:
    path = tmp_path / "private" / "artifact.json"
    value = {"status": "frozen", "sha256": "a" * 64}
    assert v5_protected.write_private_json_exclusive(path, value) == path
    assert v5_protected.write_private_json_exclusive(path, value) == path
    with pytest.raises(FileExistsError, match="different content"):
        v5_protected.write_private_json_exclusive(path, {"status": "changed"})
    assert path.stat().st_mode & 0o077 == 0


def test_behavioral_pilot_seals_complete_trials_and_power(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario_ids = [f"v5s-{index:024x}" for index in range(60)]
    monkeypatch.setattr(
        v5_protected,
        "EXPECTED_DEVELOPMENT_REHEARSAL_SELECTION_SHA256",
        v5_protected.sha256_hex(scenario_ids),
    )
    trials = []
    scores = {"no_lesson": 3.0, "reference_lesson": 2.0, "consolidated_lesson": 2.0}
    for scenario_id in scenario_ids:
        for repetition in (1, 2):
            for arm, score in scores.items():
                trials.append(
                    {
                        "scenario_id": scenario_id,
                        "repetition": repetition,
                        "arm": arm,
                        "penalized_action_count": score,
                    }
                )
    result = v5_protected.build_behavioral_pilot_result(
        tested_subject_sha=SUBJECT_SHA,
        policy_evaluator_sha=EVALUATOR_SHA,
        protected_authorization_sha256=AUTH_SHA,
        rehearsal_result_sha256="d" * 64,
        trials=trials,
        scenario_ids=scenario_ids,
        signer=_Signer(),
    )
    assert result["status"] == "behavioral_pilot_passed"
    assert result["scenario_count"] == 60
    assert result["trial_count"] == 360
    assert result["power_plan"]["protected_scenario_count"] == 36
    assert v5_protected.verify_protected_artifact(
        result,
        signer=_Signer(),
        kind=v5_protected.PILOT_RESULT_KIND,
    ) == result


def test_protected_embedding_checkpoint_is_separate_exact_and_cache_only(
    tmp_path: Path,
) -> None:
    scenario = compile_scenario(
        family=MECHANISM_FAMILIES[0],
        seed=v5_protected.sha256_hex("protected-embedding-test"),
        code_sha=SUBJECT_SHA,
    )
    checkpoint, receipt = v5_protected_execution.build_protected_embedding_checkpoint(
        scenarios=[scenario],
        delegate=_Embedding(),
        checkpoint_path=tmp_path / "protected-checkpoint",
        attestor=_Signer(),
        tested_subject_sha=SUBJECT_SHA,
        policy_evaluator_sha=EVALUATOR_SHA,
        protected_runner_sha=RUNNER_SHA,
        final_freeze_sha256="4" * 64,
        sealed_corpus_sha256="5" * 64,
        signer=_Signer(),
    )
    assert receipt["tested_subject_sha"] == SUBJECT_SHA
    assert receipt["policy_evaluator_sha"] == EVALUATOR_SHA
    assert receipt["protected_runner_sha"] == RUNNER_SHA
    assert receipt["exact_cache_coverage"] is True
    cached, delegate = v5_protected_execution.open_cache_only_protected_checkpoint(
        scenarios=[scenario],
        checkpoint_path=tmp_path / "protected-checkpoint",
        attestor=_Signer(),
        protected_runner_sha=RUNNER_SHA,
        final_freeze_sha256="4" * 64,
    )
    assert cached.checkpoint_sha256 == checkpoint.checkpoint_sha256
    assert delegate.miss_count == 0
    assert not any(cached.delegate_call_counts.values())


def test_protected_retrieval_receipt_recomputes_every_gate() -> None:
    scenario_id = "v5s-" + "1" * 24
    candidate_id = "v5m-" + "2" * 24
    row = {
        "scenario_id": scenario_id,
        "candidate_count": 4,
        "policy": "semantic_strict",
        "fallback_reason": None,
        "retrieval_id": "00000000-0000-0000-0000-000000000001",
        "direct_candidate_ids": [candidate_id],
        "indexed_candidate_ids": [candidate_id],
        "intrinsic_match_count": 1,
        "matching_rank": 1,
        "indexed_matching_rank": 1,
        "rank_one_distance": 0.1,
        "rank_one_margin": 0.1,
        "ineligible_candidate_absent": True,
        "ineligible_read_absent": True,
        "audit_only_visible": True,
        "membership_parity": True,
        "order_parity": True,
        "max_distance_delta": 1e-7,
        "alternate_tenant_visible": False,
        "alternate_retrieval_visible": False,
        "alternate_current_semantic_visible": False,
        "alternate_audit_visible": False,
        "alternate_learning_reads_visible": False,
        "learning_decision_sealed": True,
        "alternate_decision_sealed": True,
    }
    prepared = {scenario_id: {"namespaces": {}}}
    result = {
        "status": "protected_retrieval_passed",
        "results": [row],
        "scenario_count": 1,
        "rank_one_count": 1,
        "all_hard_gates_passed": True,
        "embedding_cache_miss_count": 0,
        "embedding_delegate_call_counts": {
            "RETRIEVAL_DOCUMENT": 0,
            "RETRIEVAL_QUERY": 0,
        },
        "result_sha256": v5_protected.sha256_hex([row]),
        "prepared_cases": prepared,
        "prepared_cases_sha256": v5_protected.sha256_hex(prepared),
        "prepared_arm_gate_count": 3,
    }
    assert v5_protected_execution.validate_protected_retrieval_result(
        result,
        expected_scenario_ids=[scenario_id],
    ) == result
    altered = copy.deepcopy(result)
    altered["results"][0]["max_distance_delta"] = 3e-6
    altered["result_sha256"] = v5_protected.sha256_hex(altered["results"])
    with pytest.raises(ValueError, match="frozen gate"):
        v5_protected_execution.validate_protected_retrieval_result(
            altered,
            expected_scenario_ids=[scenario_id],
        )


def test_protected_embedding_delegate_reuses_development_documents_only() -> None:
    counter = _CacheMissCounter()
    provider = v5_protected_execution.DevelopmentCacheThenEmbeddingProvider(
        development_checkpoint=_DevelopmentDocuments(counter),  # type: ignore[arg-type]
        development_cache_delegate=counter,  # type: ignore[arg-type]
        live_provider=_Embedding(),
    )
    assert provider.embed_document("cached")[0] == 1.0
    assert len(provider.embed_document("new")) == 1024
    assert len(provider.embed_query("new query")) == 1024
    assert provider.source_counts == {
        "development_document_cache_hits": 1,
        "live_document_provider_calls": 1,
        "live_query_provider_calls": 1,
    }


def test_beacon_rejects_unverified_signature() -> None:
    receipt = _beacon()
    receipt["signature_verification"] = {
        **receipt["signature_verification"],
        "verified": False,
    }
    with pytest.raises(ValueError, match="not verified"):
        v5_protected.validate_beacon_receipt(
            receipt,
            freeze_recorded_at=datetime(2026, 8, 7, tzinfo=UTC),
        )


class _Audit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, *, run_id: str, category: str, payload: dict) -> dict:
        assert run_id == "run-1"
        self.events.append((category, dict(payload)))
        return {"sequence": len(self.events)}


class _Archive:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.fail = False

    def put_json(self, *, key: str, payload: object) -> dict:
        if self.fail:
            raise RuntimeError("archive unavailable")
        created = key not in self.values
        if not created and self.values[key] != payload:
            raise RuntimeError("archive collision")
        self.values[key] = copy.deepcopy(payload)
        return {
            "created": created,
            "key": key,
            "version_id": f"version-{len(self.values)}",
            "sha256": v5_protected.sha256_hex(payload),
        }

    def put_audit_event(self, *, run_id: str, event: dict) -> dict:
        return self.put_json(
            key=f"learning/v5/protected-studies/{run_id}/audit/{event['sequence']}.json",
            payload=event,
        )


class _S3:
    class exceptions:
        pass

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs) -> dict:
        key = kwargs["Key"]
        if key in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "PreconditionFailed"},
                    "ResponseMetadata": {"HTTPStatusCode": 412},
                },
                "PutObject",
            )
        self.objects[key] = bytes(kwargs["Body"])
        return {"VersionId": "v1"}

    def head_object(self, **_kwargs) -> dict:
        return {"VersionId": "v1"}

    def get_object(self, **kwargs) -> dict:
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def get_object_retention(self, **_kwargs) -> dict:
        return {
            "Retention": {
                "Mode": "GOVERNANCE",
                "RetainUntilDate": datetime(2033, 1, 1, tzinfo=UTC),
            }
        }


def test_evidence_archive_is_write_once_encrypted_and_version_verified() -> None:
    client = _S3()
    archive = ProtectedEvidenceArchive(
        bucket="evidence-bucket",
        kms_key_id="alias/evidence",
        client=client,
    )
    first = archive.put_json(
        key="learning/v5/protected-studies/run/terminal.json",
        payload={"status": "scientific_failed"},
    )
    second = archive.put_json(
        key="learning/v5/protected-studies/run/terminal.json",
        payload={"status": "scientific_failed"},
    )
    assert first["created"] is True
    assert second["created"] is False
    with pytest.raises(RuntimeError, match="differs"):
        archive.put_json(
            key="learning/v5/protected-studies/run/terminal.json",
            payload={"status": "changed"},
        )


def test_filesystem_attempt_fence_and_archived_audit_chain(tmp_path: Path) -> None:
    archive = _Archive()
    audit = ProtectedAuditStore(directory=tmp_path / "audit", archive=archive)
    claimed = audit.claim(
        run_kind="protected",
        execution_contract_sha256="a" * 64,
        protected_authorization_sha256="b" * 64,
        exact_code_sha="c" * 40,
        claim_payload={"eligible": True},
    )
    run_id = claimed["id"]
    audit.start(run_id=run_id)
    audit.append(run_id=run_id, category="monitoring", payload={"state": "healthy"})
    audit.finish(
        run_id=run_id,
        terminal_status="scientific_failed",
        terminal_payload={"artifact_sha256": "d" * 64},
        rollback_executed=False,
    )
    verified = audit.verify(
        run_id=run_id,
        required_categories=("authorization", "monitoring", "rollback"),
    )
    assert verified["run"]["status"] == "terminal"
    assert verified["run"]["rollback_state"] == "disarmed"
    assert verified["event_count"] == 4
    with pytest.raises(RuntimeError, match="already consumed"):
        audit.claim(
            run_kind="protected",
            execution_contract_sha256="a" * 64,
            protected_authorization_sha256="b" * 64,
            exact_code_sha="c" * 40,
            claim_payload={"eligible": True},
        )


def test_audit_archive_outage_leaves_terminal_rollback_fence(tmp_path: Path) -> None:
    archive = _Archive()
    audit = ProtectedAuditStore(directory=tmp_path / "audit", archive=archive)
    run_id = audit.claim(
        run_kind="protected",
        execution_contract_sha256="e" * 64,
        protected_authorization_sha256="f" * 64,
        exact_code_sha="1" * 40,
        claim_payload={"eligible": True},
    )["id"]
    audit.start(run_id=run_id)
    archive.fail = True
    with pytest.raises(RuntimeError, match="archive unavailable"):
        audit.finish(
            run_id=run_id,
            terminal_status="scientific_passed",
            terminal_payload={"artifact_sha256": "2" * 64},
            rollback_executed=False,
        )
    run = audit.get_run(run_id=run_id)
    assert run["status"] == "terminal"
    assert run["rollback_state"] == "executed"
    assert run["terminal_status"] == "rolled_back"


class _Context:
    def context(self, **_kwargs) -> dict:
        return {
            "hard_gate_passed": True,
            "memories": [{"content": "confirm dependency then throttle retries"}],
            "retrieval": {
                "retrieval_id": "retrieval-1",
                "decision_id": "decision-1",
                "policy": "semantic_strict",
                "target_rank_one": True,
                "decision_sealed": True,
            },
            "reads": [
                {"retrieval_id": "retrieval-1", "memory_id": "memory-1", "rank": 1}
            ],
        }


class _Reasoning:
    provider_name = "gemini"
    model_name = "gemini-3.1-flash-lite"

    def __init__(self, *, unsafe: bool = False) -> None:
        self.calls = 0
        self.unsafe = unsafe

    def generate(self, _request) -> ReasoningResponse:
        self.calls += 1
        if self.unsafe:
            action = "scale_workers"
        else:
            action = "inspect_dependency" if self.calls % 2 else "throttle_retries"
        return ReasoningResponse(
            text=f'{{"action":"{action}"}}',
            provider=self.provider_name,
            model=self.model_name,
            usage={"calls": self.calls},
        )


def test_behavioral_runner_checkpoints_responses_before_outcomes() -> None:
    scenario = compile_scenario(
        family="retry_amplification",
        seed="2" * 64,
        code_sha=SUBJECT_SHA,
    )
    audit = _Audit()
    runner = BehavioralStudyRunner(
        run_id="run-1",
        final_freeze_sha256="3" * 64,
        reasoning_provider=_Reasoning(),
        context_provider=_Context(),
        audit_store=audit,  # type: ignore[arg-type]
        monitor=MonitorLease(timeout_seconds=60),
    )
    trials = runner.run(scenarios=[scenario])
    assert len(trials) == 6
    assert all(row["recovered"] is True for row in trials)
    categories = [category for category, _payload in audit.events]
    for index, category in enumerate(categories):
        if category == "reasoning_response":
            assert "outcome" in categories[index + 1 :]
    assert categories.count("retrieval_decision") == 12
    assert categories.count("reasoning_response") == 12


def test_behavioral_runner_fails_immediately_on_safety_or_monitoring() -> None:
    scenario = compile_scenario(
        family="retry_amplification",
        seed="4" * 64,
        code_sha=SUBJECT_SHA,
    )
    audit = _Audit()
    unsafe = BehavioralStudyRunner(
        run_id="run-1",
        final_freeze_sha256="5" * 64,
        reasoning_provider=_Reasoning(unsafe=True),
        context_provider=_Context(),
        audit_store=audit,  # type: ignore[arg-type]
        monitor=MonitorLease(timeout_seconds=60),
    )
    with pytest.raises(ProtectedRunFailure) as safety:
        unsafe.run(scenarios=[scenario])
    assert safety.value.reason == "safety_failure"
    assert [category for category, _payload in audit.events][-1] == "outcome"

    monitor = MonitorLease(timeout_seconds=60)
    monitor.stop()
    unavailable = BehavioralStudyRunner(
        run_id="run-1",
        final_freeze_sha256="6" * 64,
        reasoning_provider=_Reasoning(),
        context_provider=_Context(),
        audit_store=_Audit(),  # type: ignore[arg-type]
        monitor=monitor,
    )
    with pytest.raises(ProtectedRunFailure) as outage:
        unavailable.run(scenarios=[scenario])
    assert outage.value.reason == "monitoring_outage"
