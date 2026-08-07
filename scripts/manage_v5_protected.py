"""Manage the fail-closed stages of the V5 protected study."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hindsight.opaque_tokens import KmsHmacTokenizer  # noqa: E402
from hindsight.embedding_index import activate_profile, begin_profile_build  # noqa: E402
from hindsight.memory import MemoryStore  # noqa: E402
from hindsight.server_tenants import learning_tenant_id  # noqa: E402
from hindsight.tenant import tenant_scope  # noqa: E402
from hindsight.v5_governance import (  # noqa: E402
    CacheOnlyEmbeddingProvider,
    V1_QUALIFICATION_CONTRACT_SHA256,
    governance_v2_policy,
    verify_governance_v2,
    verify_protected_learning_authorization,
    verify_rehearsal_result,
)
from hindsight.v5_protected import (  # noqa: E402
    CORPUS_SEAL_KIND,
    EMBEDDING_CHECKPOINT_KIND,
    FINAL_FREEZE_KIND,
    PILOT_RESULT_KIND,
    RETRIEVAL_RESULT_KIND,
    TERMINAL_RESULT_KIND,
    build_behavioral_pilot_result,
    build_final_freeze,
    derive_protected_corpus,
    evaluate_terminal_result,
    new_review_state,
    next_review_item,
    owner_review_guide,
    persist_protected_corpus_items,
    protected_study_protocol,
    record_review_event,
    seal_reviewed_corpus,
    sign_protected_artifact,
    validate_beacon_receipt,
    validate_terminal_artifact,
    verify_protected_artifact,
    write_private_json_exclusive,
)
from hindsight.v5_protected_execution import (  # noqa: E402
    DevelopmentCacheThenEmbeddingProvider,
    PROTECTED_DATABASE_RE,
    build_protected_embedding_checkpoint,
    open_cache_only_protected_checkpoint,
    qualify_protected_retrieval,
    validate_protected_retrieval_result,
)
from hindsight.v5_protected_runtime import (  # noqa: E402
    BehavioralStudyRunner,
    DatabaseArmContextProvider,
    LeaseHeartbeat,
    MonitorLease,
    ProtectedAuditStore,
    ProtectedEvidenceArchive,
    ProtectedRunFailure,
    prepare_arm_database,
)
from hindsight.v5_qualification import (  # noqa: E402
    CheckpointedEmbeddingProvider,
    _initialize_exact_profile,
    qualify_development_structure,
    require_fresh_development_database,
    require_restricted_runtime_database,
    select_embedding_scenarios,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("protocol")

    source = commands.add_parser("verify-source")
    _add_source_arguments(source)

    pilot = commands.add_parser("run-pilot")
    _add_source_arguments(pilot)
    pilot.add_argument("--checkpoint", type=pathlib.Path, required=True)
    pilot.add_argument("--execution-manifest", type=pathlib.Path, required=True)
    pilot.add_argument("--output", type=pathlib.Path, required=True)

    freeze = commands.add_parser("freeze")
    _add_source_arguments(freeze)
    freeze.add_argument("--pilot", type=pathlib.Path, required=True)
    freeze.add_argument("--output", type=pathlib.Path, required=True)

    construct = commands.add_parser("construct-corpus")
    construct.add_argument("--freeze", type=pathlib.Path, required=True)
    construct.add_argument("--beacon", type=pathlib.Path, required=True)
    construct.add_argument("--items-directory", type=pathlib.Path, required=True)
    construct.add_argument("--output", type=pathlib.Path, required=True)

    review_init = commands.add_parser("review-init")
    review_init.add_argument("--corpus", type=pathlib.Path, required=True)
    review_init.add_argument("--owner", required=True)
    review_init.add_argument("--output", type=pathlib.Path, required=True)

    review_next = commands.add_parser("review-next")
    _add_review_arguments(review_next)

    review_record = commands.add_parser("review-record")
    _add_review_arguments(review_record)
    review_record.add_argument("--action", choices=("approve", "reject", "clarify"), required=True)
    review_record.add_argument("--scenario-id", required=True)
    review_record.add_argument("--output", type=pathlib.Path, required=True)

    seal = commands.add_parser("seal-corpus")
    _add_review_arguments(seal)
    seal.add_argument("--output", type=pathlib.Path, required=True)

    embeddings = commands.add_parser("build-embeddings")
    _add_source_arguments(embeddings)
    _add_protected_chain_arguments(embeddings, include_retrieval=False)
    embeddings.add_argument("--checkpoint", type=pathlib.Path, required=True)
    embeddings.add_argument("--development-checkpoint", type=pathlib.Path, required=True)
    embeddings.add_argument(
        "--development-execution-manifest", type=pathlib.Path, required=True
    )
    embeddings.add_argument("--output", type=pathlib.Path, required=True)

    qualify = commands.add_parser("qualify-retrieval")
    _add_source_arguments(qualify)
    _add_protected_chain_arguments(qualify, include_retrieval=False)
    qualify.add_argument("--checkpoint", type=pathlib.Path, required=True)
    qualify.add_argument("--embedding-receipt", type=pathlib.Path, required=True)
    qualify.add_argument("--output", type=pathlib.Path, required=True)

    execute = commands.add_parser("run-protected")
    _add_source_arguments(execute)
    _add_protected_chain_arguments(execute, include_retrieval=True)
    execute.add_argument("--checkpoint", type=pathlib.Path, required=True)
    execute.add_argument("--embedding-receipt", type=pathlib.Path, required=True)
    execute.add_argument("--output", type=pathlib.Path, required=True)

    verify = commands.add_parser("verify-artifact")
    verify.add_argument("--artifact", type=pathlib.Path, required=True)
    verify.add_argument(
        "--kind",
        choices=(
            FINAL_FREEZE_KIND,
            CORPUS_SEAL_KIND,
            EMBEDDING_CHECKPOINT_KIND,
            RETRIEVAL_RESULT_KIND,
            TERMINAL_RESULT_KIND,
        ),
        required=True,
    )

    verify_terminal = commands.add_parser("verify-terminal")
    verify_terminal.add_argument("--terminal", type=pathlib.Path, required=True)
    verify_terminal.add_argument("--freeze", type=pathlib.Path, required=True)
    verify_terminal.add_argument("--sealed-corpus", type=pathlib.Path, required=True)

    args = parser.parse_args()
    if args.command == "protocol":
        _print_json(protected_study_protocol())
        return 0
    governance_signer, protected_signer = _signers()
    if args.command == "verify-artifact":
        value = verify_protected_artifact(
            _load_json(args.artifact), signer=protected_signer, kind=args.kind
        )
        _print_json(_summary(value))
        return 0
    if args.command == "verify-terminal":
        frozen = verify_protected_artifact(
            _load_json(args.freeze), signer=protected_signer, kind=FINAL_FREEZE_KIND
        )
        sealed = verify_protected_artifact(
            _load_json(args.sealed_corpus), signer=protected_signer, kind=CORPUS_SEAL_KIND
        )
        terminal = verify_protected_artifact(
            _load_json(args.terminal), signer=protected_signer, kind=TERMINAL_RESULT_KIND
        )
        value = validate_terminal_artifact(
            terminal=terminal,
            final_freeze=frozen,
            sealed_corpus=sealed,
        )
        _print_json(_summary(value))
        return 0
    if args.command == "verify-source":
        value = _verify_source_chain(args, signer=governance_signer)
        _print_json(value)
        return 0
    if args.command == "run-pilot":
        source_chain = _verify_source_chain(args, signer=governance_signer)
        value = _run_pilot(
            args=args,
            source=source_chain,
            protected_signer=protected_signer,
        )
        _print_json(_summary(value))
        return 0
    if args.command == "freeze":
        source_chain = _verify_source_chain(args, signer=governance_signer)
        pilot = verify_protected_artifact(
            _load_json(args.pilot), signer=protected_signer, kind=PILOT_RESULT_KIND
        )
        if (
            pilot.get("status") != "behavioral_pilot_passed"
            or pilot.get("tested_subject_sha") != source_chain["tested_subject_sha"]
            or pilot.get("policy_evaluator_sha") != source_chain["policy_evaluator_sha"]
            or pilot.get("protected_authorization_sha256")
            != source_chain["protected_authorization_sha256"]
        ):
            raise ValueError("v5 protected pilot does not bind the verified source chain")
        value = build_final_freeze(
            tested_subject_sha=source_chain["tested_subject_sha"],
            policy_evaluator_sha=source_chain["policy_evaluator_sha"],
            protected_runner_sha=_exact_main_sha(),
            source_protected_authorization_sha256=source_chain[
                "protected_authorization_sha256"
            ],
            source_pilot_sha256=pilot["artifact_sha256"],
            power_plan=pilot["power_plan"],
            recorded_at=datetime.now(UTC).isoformat(),
            signer=protected_signer,
        )
        _write_signed(args.output, value, stage="freeze")
    elif args.command == "construct-corpus":
        frozen = verify_protected_artifact(
            _load_json(args.freeze), signer=protected_signer, kind=FINAL_FREEZE_KIND
        )
        if frozen.get("protected_runner_sha") != _exact_main_sha():
            raise ValueError("v5 protected freeze differs from exact main")
        beacon = _load_json(args.beacon)
        validate_beacon_receipt(
            beacon,
            freeze_recorded_at=datetime.fromisoformat(str(frozen["recorded_at"])),
        )
        value = derive_protected_corpus(final_freeze=frozen, beacon=beacon)
        persist_protected_corpus_items(corpus=value, directory=args.items_directory)
        _write(args.output, value)
    elif args.command == "review-init":
        value = new_review_state(corpus=_load_json(args.corpus), owner=args.owner)
        _write(args.output, value)
    elif args.command == "review-next":
        value = {
            "guide": owner_review_guide(),
            "item": next_review_item(
                corpus=_load_json(args.corpus), state=_load_json(args.state)
            ),
        }
        _print_json(value)
        return 0
    elif args.command == "review-record":
        value = record_review_event(
            corpus=_load_json(args.corpus),
            state=_load_json(args.state),
            action=args.action,
            scenario_id=args.scenario_id,
            recorded_at=datetime.now(UTC).isoformat(),
        )
        _write(args.output, value)
    elif args.command == "seal-corpus":
        value = seal_reviewed_corpus(
            corpus=_load_json(args.corpus),
            state=_load_json(args.state),
            signer=protected_signer,
        )
        _write_signed(args.output, value, stage="corpus-seal")
    elif args.command == "build-embeddings":
        source = _verify_source_chain(args, signer=governance_signer)
        frozen, sealed = _verify_protected_chain(args, signer=protected_signer)
        _require_chain_subject(source=source, frozen=frozen)
        development_delegate = CacheOnlyEmbeddingProvider()
        development_checkpoint = CheckpointedEmbeddingProvider(
            development_delegate,
            args.development_checkpoint,
            code_sha=source["tested_subject_sha"],
            attestor=KmsHmacTokenizer(
                key_id=protected_signer.key_id,
                family_sha256=V1_QUALIFICATION_CONTRACT_SHA256,
            ),
            execution_manifest_sha256=str(
                _load_json(args.development_execution_manifest)[
                    "execution_manifest_sha256"
                ]
            ),
            qualification_contract_sha256=V1_QUALIFICATION_CONTRACT_SHA256,
        )
        if development_checkpoint.checkpoint_sha256 != _load_json(args.diagnostic).get(
            "checkpoint_sha256"
        ):
            raise ValueError("v5 protected development checkpoint differs")
        provider = DevelopmentCacheThenEmbeddingProvider(
            development_checkpoint=development_checkpoint,
            development_cache_delegate=development_delegate,
            live_provider=_gemini_embedding_provider(),
        )
        checkpoint, value = build_protected_embedding_checkpoint(
            scenarios=sealed["selected_scenarios"],
            delegate=provider,
            checkpoint_path=args.checkpoint,
            attestor=protected_signer,
            tested_subject_sha=frozen["tested_subject_sha"],
            policy_evaluator_sha=frozen["policy_evaluator_sha"],
            protected_runner_sha=frozen["protected_runner_sha"],
            final_freeze_sha256=frozen["artifact_sha256"],
            sealed_corpus_sha256=sealed["artifact_sha256"],
            signer=protected_signer,
            progress_callback=_progress,
        )
        if not checkpoint.checkpoint_sha256:
            raise RuntimeError("v5 protected checkpoint identity is absent")
        _write_signed(args.output, value, stage="embedding-checkpoint")
    elif args.command == "qualify-retrieval":
        source = _verify_source_chain(args, signer=governance_signer)
        frozen, sealed = _verify_protected_chain(args, signer=protected_signer)
        _require_chain_subject(source=source, frozen=frozen)
        embedding_receipt = verify_protected_artifact(
            _load_json(args.embedding_receipt),
            signer=protected_signer,
            kind=EMBEDDING_CHECKPOINT_KIND,
        )
        if (
            embedding_receipt.get("status") != "protected_embeddings_ready"
            or embedding_receipt.get("tested_subject_sha") != frozen["tested_subject_sha"]
            or embedding_receipt.get("policy_evaluator_sha")
            != frozen["policy_evaluator_sha"]
            or embedding_receipt.get("protected_runner_sha")
            != frozen["protected_runner_sha"]
            or embedding_receipt.get("final_freeze_sha256") != frozen["artifact_sha256"]
            or embedding_receipt.get("sealed_corpus_sha256") != sealed["artifact_sha256"]
        ):
            raise ValueError("v5 protected embedding receipt differs")
        checkpoint, delegate = open_cache_only_protected_checkpoint(
            scenarios=sealed["selected_scenarios"],
            checkpoint_path=args.checkpoint,
            attestor=protected_signer,
            protected_runner_sha=frozen["protected_runner_sha"],
            final_freeze_sha256=frozen["artifact_sha256"],
        )
        if checkpoint.checkpoint_sha256 != embedding_receipt["checkpoint_sha256"]:
            raise ValueError("v5 protected checkpoint differs from its signed receipt")
        database_url, runtime_database_url = _database_urls()
        value = qualify_protected_retrieval(
            scenarios=sealed["selected_scenarios"],
            checkpoint=checkpoint,
            cache_only_delegate=delegate,
            tested_subject_sha=frozen["tested_subject_sha"],
            policy_evaluator_sha=frozen["policy_evaluator_sha"],
            protected_runner_sha=frozen["protected_runner_sha"],
            final_freeze_sha256=frozen["artifact_sha256"],
            sealed_corpus_sha256=sealed["artifact_sha256"],
            embedding_checkpoint_sha256=embedding_receipt["artifact_sha256"],
            database_url=database_url,
            runtime_database_url=runtime_database_url,
            signer=protected_signer,
            progress_callback=_progress,
        )
        _write_signed(args.output, value, stage="retrieval-qualification")
    elif args.command == "run-protected":
        source = _verify_source_chain(args, signer=governance_signer)
        frozen, sealed = _verify_protected_chain(args, signer=protected_signer)
        _require_chain_subject(source=source, frozen=frozen)
        value = _run_protected(
            args=args,
            source=source,
            frozen=frozen,
            sealed=sealed,
            protected_signer=protected_signer,
        )
        _print_json(_summary(value))
        return 0
    else:
        raise AssertionError(f"unsupported v5 protected command: {args.command}")
    _print_json(_summary(value))
    return 0


def _run_pilot(
    *,
    args: argparse.Namespace,
    source: dict[str, Any],
    protected_signer: KmsHmacTokenizer,
) -> dict[str, Any]:
    exact_main = _exact_main_sha()
    database_url, runtime_database_url = _database_urls()
    pilot_pattern = re.compile(r"hindsight_v5_pilot_[a-z0-9_]+")
    require_fresh_development_database(
        database_url,
        database_name_pattern=pilot_pattern,
    )
    require_restricted_runtime_database(
        database_url,
        runtime_database_url,
        database_name_pattern=pilot_pattern,
    )
    diagnostic = _load_json(args.diagnostic)
    execution_manifest = _load_json(args.execution_manifest)
    checkpoint_attestor = KmsHmacTokenizer(
        key_id=protected_signer.key_id,
        family_sha256=V1_QUALIFICATION_CONTRACT_SHA256,
    )
    delegate = CacheOnlyEmbeddingProvider()
    checkpoint = CheckpointedEmbeddingProvider(
        delegate,
        args.checkpoint,
        code_sha=source["tested_subject_sha"],
        attestor=checkpoint_attestor,
        execution_manifest_sha256=str(execution_manifest["execution_manifest_sha256"]),
        qualification_contract_sha256=V1_QUALIFICATION_CONTRACT_SHA256,
    )
    if checkpoint.checkpoint_sha256 != diagnostic.get("checkpoint_sha256"):
        raise ValueError("v5 pilot development checkpoint differs")
    selected = select_embedding_scenarios(code_sha=source["tested_subject_sha"])
    rehearsal_ids = list(
        qualify_development_structure(code_sha=source["tested_subject_sha"])[
            "rehearsal_scenario_ids"
        ]
    )
    scenarios_by_id = {str(row["scenario_id"]): row for row in selected}
    scenarios = [scenarios_by_id[scenario_id] for scenario_id in rehearsal_ids]
    _exercise_checkpoint(checkpoint=checkpoint, scenarios=scenarios)
    if delegate.miss_count or any(checkpoint.delegate_call_counts.values()):
        raise RuntimeError("v5 pilot cache preflight attempted an embedding provider call")
    _initialize_exact_profile(
        provider=checkpoint,
        db_url=database_url,
        store_factory=MemoryStore,
        begin_profile_build_fn=begin_profile_build,
        activate_profile_fn=activate_profile,
    )
    with tenant_scope(learning_tenant_id()):
        prepared = prepare_arm_database(
            scenarios=scenarios,
            db_url=runtime_database_url,
            provider=checkpoint,
            execution_contract_sha256=protected_study_protocol()["protocol_sha256"],
            phase="development-pilot",
        )
    if delegate.miss_count or any(checkpoint.delegate_call_counts.values()):
        raise RuntimeError("v5 pilot preparation attempted an embedding provider call")
    audit = ProtectedAuditStore(
        directory=_protected_state_directory(exact_main),
        archive=_evidence_archive(),
    )
    with tenant_scope(learning_tenant_id()):
        claimed = audit.claim(
            run_kind="development_pilot",
            execution_contract_sha256=protected_study_protocol()["protocol_sha256"],
            protected_authorization_sha256=source["protected_authorization_sha256"],
            exact_code_sha=exact_main,
            claim_payload={
                "source": source,
                "scenario_ids": rehearsal_ids,
                "checkpoint_sha256": checkpoint.checkpoint_sha256,
                "embedding_mode": "cache-only",
            },
        )
        run_id = str(claimed["id"])
        audit.start(run_id=run_id)
        monitor = MonitorLease(timeout_seconds=15)
        context = DatabaseArmContextProvider(
            db_url=runtime_database_url,
            provider=checkpoint,
            prepared_cases=prepared,
            cache_only_delegate=delegate,
            phase="development-pilot",
        )
        runner = BehavioralStudyRunner(
            run_id=run_id,
            final_freeze_sha256=protected_study_protocol()["protocol_sha256"],
            reasoning_provider=_gemini_reasoning_provider(),
            context_provider=context,
            audit_store=audit,
            monitor=monitor,
            stop_on_unsafe=False,
            progress_callback=_trial_progress,
        )
        try:
            with LeaseHeartbeat(
                lease=monitor,
                health_check=lambda: _monitor_run(audit=audit, run_id=run_id),
            ):
                trials = runner.run(scenarios=scenarios)
            value = build_behavioral_pilot_result(
                tested_subject_sha=source["tested_subject_sha"],
                policy_evaluator_sha=source["policy_evaluator_sha"],
                protected_authorization_sha256=source[
                    "protected_authorization_sha256"
                ],
                rehearsal_result_sha256=source["rehearsal_sha256"],
                trials=trials,
                scenario_ids=rehearsal_ids,
                signer=protected_signer,
            )
            verification = audit.verify(
                run_id=run_id,
                required_categories=(
                    "authorization",
                    "retrieval_decision",
                    "memory_read",
                    "reasoning_response",
                    "outcome",
                    "rollback",
                    "monitoring",
                ),
            )
            _archive_terminal(run_id=run_id, value=value)
            audit.finish(
                run_id=run_id,
                terminal_status=str(value["status"]),
                terminal_payload={
                    "artifact_sha256": value["artifact_sha256"],
                    "audit_head_before_terminal": verification["audit_head_sha256"],
                },
                rollback_executed=False,
            )
        except Exception as exc:
            value = _failed_artifact(
                kind=PILOT_RESULT_KIND,
                signer=protected_signer,
                run_id=run_id,
                reason=_failure_reason(exc),
                exact_code_sha=exact_main,
                source=source,
            )
            _write(args.output, value)
            try:
                _archive_terminal(run_id=run_id, value=value)
            finally:
                if audit.get_run(run_id=run_id)["status"] != "terminal":
                    audit.finish(
                        run_id=run_id,
                        terminal_status="pilot_rolled_back",
                        terminal_payload={"artifact_sha256": value["artifact_sha256"]},
                        rollback_executed=True,
                        rollback_reason=_failure_reason(exc),
                    )
        audit.verify(run_id=run_id, required_categories=("authorization", "rollback"))
    _write(args.output, value)
    return value


def _run_protected(
    *,
    args: argparse.Namespace,
    source: dict[str, Any],
    frozen: dict[str, Any],
    sealed: dict[str, Any],
    protected_signer: KmsHmacTokenizer,
) -> dict[str, Any]:
    exact_main = _exact_main_sha()
    embedding_receipt = verify_protected_artifact(
        _load_json(args.embedding_receipt),
        signer=protected_signer,
        kind=EMBEDDING_CHECKPOINT_KIND,
    )
    retrieval = verify_protected_artifact(
        _load_json(args.retrieval_result),
        signer=protected_signer,
        kind=RETRIEVAL_RESULT_KIND,
    )
    scenarios = list(sealed["selected_scenarios"])
    validate_protected_retrieval_result(
        retrieval,
        expected_scenario_ids=[str(row["scenario_id"]) for row in scenarios],
    )
    if (
        embedding_receipt.get("status") != "protected_embeddings_ready"
        or embedding_receipt.get("tested_subject_sha") != frozen["tested_subject_sha"]
        or embedding_receipt.get("policy_evaluator_sha") != frozen["policy_evaluator_sha"]
        or embedding_receipt.get("protected_runner_sha") != frozen["protected_runner_sha"]
        or embedding_receipt.get("final_freeze_sha256") != frozen["artifact_sha256"]
        or embedding_receipt.get("sealed_corpus_sha256") != sealed["artifact_sha256"]
        or retrieval.get("status") != "protected_retrieval_passed"
        or retrieval.get("all_hard_gates_passed") is not True
        or retrieval.get("embedding_cache_miss_count") != 0
        or retrieval.get("embedding_checkpoint_sha256")
        != embedding_receipt["artifact_sha256"]
        or retrieval.get("tested_subject_sha") != frozen["tested_subject_sha"]
        or retrieval.get("policy_evaluator_sha") != frozen["policy_evaluator_sha"]
        or retrieval.get("protected_runner_sha") != frozen["protected_runner_sha"]
        or retrieval.get("prepared_arm_gate_count") != len(scenarios) * 3
        or retrieval.get("prepared_cases_sha256")
        != _sha256(retrieval.get("prepared_cases"))
    ):
        raise ValueError("v5 protected execution evidence is not eligible")
    checkpoint, delegate = open_cache_only_protected_checkpoint(
        scenarios=scenarios,
        checkpoint_path=args.checkpoint,
        attestor=protected_signer,
        protected_runner_sha=frozen["protected_runner_sha"],
        final_freeze_sha256=frozen["artifact_sha256"],
    )
    if checkpoint.checkpoint_sha256 != embedding_receipt["checkpoint_sha256"]:
        raise ValueError("v5 protected execution checkpoint differs")
    database_url, runtime_database_url = _database_urls()
    _verify_protected_database_binding(
        database_url=database_url,
        runtime_database_url=runtime_database_url,
        retrieval=retrieval,
    )
    prepared = retrieval["prepared_cases"]
    if set(prepared) != {str(row["scenario_id"]) for row in scenarios}:
        raise ValueError("v5 protected prepared arm identities differ")
    audit = ProtectedAuditStore(
        directory=_protected_state_directory(exact_main),
        archive=_evidence_archive(),
    )
    with tenant_scope(learning_tenant_id()):
        claimed = audit.claim(
            run_kind="protected",
            execution_contract_sha256=frozen["artifact_sha256"],
            protected_authorization_sha256=source["protected_authorization_sha256"],
            exact_code_sha=exact_main,
            claim_payload={
                "source": source,
                "final_freeze_sha256": frozen["artifact_sha256"],
                "sealed_corpus_sha256": sealed["artifact_sha256"],
                "embedding_receipt_sha256": embedding_receipt["artifact_sha256"],
                "retrieval_result_sha256": retrieval["artifact_sha256"],
                "embedding_mode": "cache-only",
            },
        )
        run_id = str(claimed["id"])
        audit.start(run_id=run_id)
        monitor = MonitorLease(timeout_seconds=15)
        runner = BehavioralStudyRunner(
            run_id=run_id,
            final_freeze_sha256=frozen["artifact_sha256"],
            reasoning_provider=_gemini_reasoning_provider(),
            context_provider=DatabaseArmContextProvider(
                db_url=runtime_database_url,
                provider=checkpoint,
                prepared_cases=prepared,
                cache_only_delegate=delegate,
            ),
            audit_store=audit,
            monitor=monitor,
            progress_callback=_trial_progress,
        )
        try:
            with LeaseHeartbeat(
                lease=monitor,
                health_check=lambda: _monitor_run(audit=audit, run_id=run_id),
            ):
                trials = runner.run(scenarios=scenarios)
            unsigned = evaluate_terminal_result(
                final_freeze=frozen,
                sealed_corpus=sealed,
                trials=trials,
                hard_gates_passed=True,
                rollback_state="disarmed",
                embedding_cache_miss_count=delegate.miss_count,
            )
            verification = audit.verify(
                run_id=run_id,
                required_categories=(
                    "authorization",
                    "retrieval_decision",
                    "memory_read",
                    "reasoning_response",
                    "outcome",
                    "rollback",
                    "monitoring",
                ),
            )
            value = sign_protected_artifact(
                {
                    **unsigned,
                    "run_id": run_id,
                    "exact_code_sha": exact_main,
                    "protected_authorization_sha256": source[
                        "protected_authorization_sha256"
                    ],
                    "embedding_checkpoint_sha256": embedding_receipt[
                        "artifact_sha256"
                    ],
                    "retrieval_result_sha256": retrieval["artifact_sha256"],
                    "trials": trials,
                    "trials_sha256": _sha256(trials),
                    "audit_head_before_terminal": verification["audit_head_sha256"],
                    "audit_event_count_before_terminal": verification["event_count"],
                },
                signer=protected_signer,
                kind=TERMINAL_RESULT_KIND,
            )
            _archive_terminal(run_id=run_id, value=value)
            audit.finish(
                run_id=run_id,
                terminal_status=str(value["status"]),
                terminal_payload={"artifact_sha256": value["artifact_sha256"]},
                rollback_executed=False,
            )
        except Exception as exc:
            value = _failed_artifact(
                kind=TERMINAL_RESULT_KIND,
                signer=protected_signer,
                run_id=run_id,
                reason=_failure_reason(exc),
                exact_code_sha=exact_main,
                source={
                    **source,
                    "final_freeze_sha256": frozen["artifact_sha256"],
                    "sealed_corpus_sha256": sealed["artifact_sha256"],
                },
            )
            _write(args.output, value)
            try:
                _archive_terminal(run_id=run_id, value=value)
            finally:
                if audit.get_run(run_id=run_id)["status"] != "terminal":
                    audit.finish(
                        run_id=run_id,
                        terminal_status="rolled_back",
                        terminal_payload={"artifact_sha256": value["artifact_sha256"]},
                        rollback_executed=True,
                        rollback_reason=_failure_reason(exc),
                    )
        audit.verify(run_id=run_id, required_categories=("authorization", "rollback"))
    _write(args.output, value)
    return value


def _add_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--diagnostic", type=pathlib.Path, required=True)
    parser.add_argument("--authorization", type=pathlib.Path, required=True)
    parser.add_argument("--rehearsal", type=pathlib.Path, required=True)
    parser.add_argument("--protected-authorization", type=pathlib.Path, required=True)


def _add_protected_chain_arguments(
    parser: argparse.ArgumentParser, *, include_retrieval: bool
) -> None:
    parser.add_argument("--freeze", type=pathlib.Path, required=True)
    parser.add_argument("--sealed-corpus", type=pathlib.Path, required=True)
    if include_retrieval:
        parser.add_argument("--retrieval-result", type=pathlib.Path, required=True)


def _add_review_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--corpus", type=pathlib.Path, required=True)
    parser.add_argument("--state", type=pathlib.Path, required=True)


def _verify_source_chain(args: argparse.Namespace, *, signer: KmsHmacTokenizer) -> dict[str, Any]:
    diagnostic_raw = args.diagnostic.read_bytes()
    diagnostic = json.loads(diagnostic_raw)
    authorization = _load_json(args.authorization)
    rehearsal = _load_json(args.rehearsal)
    protected = _load_json(args.protected_authorization)
    verified_authorization = verify_governance_v2(
        authorization=authorization,
        diagnostic=diagnostic,
        expected_diagnostic_sha256=str(authorization["source_diagnostic_sha256"]),
        diagnostic_file_sha256=hashlib.sha256(diagnostic_raw).hexdigest(),
        tested_subject_sha=str(authorization["tested_subject_sha"]),
        policy_evaluator_sha=str(authorization["policy_evaluator_sha"]),
        signer=signer,
    )
    verify_rehearsal_result(rehearsal_result=rehearsal, signer=signer)
    verify_protected_learning_authorization(protected_authorization=protected, signer=signer)
    if (
        rehearsal.get("source_authorization_sha256")
        != verified_authorization["artifact_sha256"]
        or protected.get("source_authorization_sha256")
        != verified_authorization["artifact_sha256"]
        or protected.get("source_rehearsal_sha256") != rehearsal.get("artifact_sha256")
        or protected.get("tested_subject_sha") != verified_authorization["tested_subject_sha"]
        or protected.get("policy_evaluator_sha")
        != verified_authorization["policy_evaluator_sha"]
    ):
        raise ValueError("v5 protected source artifact chain differs")
    return {
        "status": "source_chain_verified",
        "tested_subject_sha": protected["tested_subject_sha"],
        "policy_evaluator_sha": protected["policy_evaluator_sha"],
        "authorization_sha256": verified_authorization["artifact_sha256"],
        "rehearsal_sha256": rehearsal["artifact_sha256"],
        "protected_authorization_sha256": protected["artifact_sha256"],
    }


def _verify_protected_chain(
    args: argparse.Namespace, *, signer: KmsHmacTokenizer
) -> tuple[dict[str, Any], dict[str, Any]]:
    frozen = verify_protected_artifact(
        _load_json(args.freeze), signer=signer, kind=FINAL_FREEZE_KIND
    )
    sealed = verify_protected_artifact(
        _load_json(args.sealed_corpus), signer=signer, kind=CORPUS_SEAL_KIND
    )
    if (
        frozen.get("status") != "study_frozen"
        or frozen.get("protected_runner_sha") != _exact_main_sha()
        or sealed.get("status") != "protected_corpus_sealed"
        or sealed.get("final_freeze_sha256") != frozen.get("artifact_sha256")
    ):
        raise ValueError("v5 protected artifact chain differs")
    return frozen, sealed


def _require_chain_subject(*, source: dict[str, Any], frozen: dict[str, Any]) -> None:
    if (
        frozen.get("tested_subject_sha") != source["tested_subject_sha"]
        or frozen.get("policy_evaluator_sha") != source["policy_evaluator_sha"]
        or frozen.get("source_protected_authorization_sha256")
        != source["protected_authorization_sha256"]
    ):
        raise ValueError("v5 protected freeze source identity differs")


def _exercise_checkpoint(
    *, checkpoint: CheckpointedEmbeddingProvider, scenarios: list[dict[str, Any]]
) -> None:
    from hindsight.v5_qualification import (
        _candidate_memories,
        render_retrieval_document,
        render_retrieval_query,
    )

    for scenario in scenarios:
        for memory in _candidate_memories(scenario):
            checkpoint.embed_document(render_retrieval_document(memory))
        query = render_retrieval_query(scenario)
        with checkpoint.query_scope(scenario_id=str(scenario["scenario_id"]), query=query):
            checkpoint.embed_query(query)


def _gemini_reasoning_provider() -> Any:
    from hindsight.gemini import gemini_pool_from_env
    from hindsight.reasoning import GeminiReasoningProvider, RetryingReasoningProvider
    from hindsight.runtime import runtime_settings
    from hindsight.v5_corpus import REASONING_MODEL

    provider_env = {
        **os.environ,
        "LLM_PROVIDER": "gemini",
        "GEMINI_MODEL": REASONING_MODEL,
    }
    settings = runtime_settings(environ=provider_env, use_cache=False)
    provider = GeminiReasoningProvider(
        credential_pool=gemini_pool_from_env(settings.provider_env),
        model_name=REASONING_MODEL,
    )
    return RetryingReasoningProvider(provider, max_attempts=4)


def _monitor_run(*, audit: ProtectedAuditStore, run_id: str) -> None:
    with tenant_scope(learning_tenant_id()):
        run = audit.get_run(run_id=run_id)
    if run.get("status") != "running" or run.get("rollback_state") != "armed":
        raise RuntimeError("v5 protected monitored run state differs")


def _verify_protected_database_binding(
    *, database_url: str, runtime_database_url: str, retrieval: dict[str, Any]
) -> None:
    from hindsight.db import connect

    identities = require_restricted_runtime_database(
        database_url,
        runtime_database_url,
        database_name_pattern=PROTECTED_DATABASE_RE,
    )
    with connect(
        database_url,
        application_name="hindsight-v5-protected-execution-binding",
    ) as connection:
        row = connection.execute(
            "SELECT current_database(), crdb_internal.cluster_id()::STRING"
        ).fetchone()
    if row is None:
        raise RuntimeError("v5 protected execution database identity is absent")
    database_name, cluster_id = (str(value) for value in row)
    if (
        retrieval.get("database_name") != database_name
        or retrieval.get("database_cluster_id_sha256")
        != hashlib.sha256(cluster_id.encode()).hexdigest()
        or retrieval.get("deploy_database_identity_sha256")
        != hashlib.sha256(identities["deploy_identity"].encode()).hexdigest()
        or retrieval.get("runtime_database_identity_sha256")
        != hashlib.sha256(identities["runtime_identity"].encode()).hexdigest()
    ):
        raise ValueError("v5 protected execution database binding differs")


def _archive_terminal(*, run_id: str, value: dict[str, Any]) -> dict[str, Any]:
    return _archive_value(
        key=(
            f"learning/v5/protected-studies/{run_id}/terminal/"
            f"{value['artifact_sha256']}.json"
        ),
        value=value,
    )


def _archive_value(*, key: str, value: dict[str, Any]) -> dict[str, Any]:
    return _evidence_archive().put_json(key=key, payload=value)


def _evidence_archive() -> ProtectedEvidenceArchive:
    import boto3

    from hindsight.aws import aws_client_config

    bucket = (os.environ.get("HINDSIGHT_EVIDENCE_BUCKET") or "").strip()
    kms_key_id = (os.environ.get("HINDSIGHT_EVIDENCE_KMS_KEY_ID") or "").strip()
    if not bucket or not kms_key_id:
        raise RuntimeError(
            "HINDSIGHT_EVIDENCE_BUCKET and HINDSIGHT_EVIDENCE_KMS_KEY_ID are required"
        )
    return ProtectedEvidenceArchive(
        bucket=bucket,
        kms_key_id=kms_key_id,
        client=boto3.client("s3", config=aws_client_config()),
    )


def _protected_state_directory(exact_code_sha: str) -> pathlib.Path:
    configured = (os.environ.get("HINDSIGHT_V5_PROTECTED_STATE_DIR") or "").strip()
    root = (
        pathlib.Path(configured).expanduser()
        if configured
        else pathlib.Path.home() / ".local" / "state" / "hindsight" / "v5" / "protected-study"
    )
    return root / exact_code_sha


def _failed_artifact(
    *,
    kind: str,
    signer: KmsHmacTokenizer,
    run_id: str,
    reason: str,
    exact_code_sha: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    return sign_protected_artifact(
        {
            "schema_version": 1,
            "status": "rolled_back",
            "claim_authorized": False,
            "run_id": run_id,
            "exact_code_sha": exact_code_sha,
            "rollback_state": "executed",
            "rollback_reason": reason,
            "source": source,
        },
        signer=signer,
        kind=kind,
    )


def _failure_reason(exc: Exception) -> str:
    if isinstance(exc, ProtectedRunFailure):
        return exc.reason
    message = str(exc).lower()
    if "cache" in message:
        return "embedding_cache_miss"
    if "signature" in message or "hmac" in message:
        return "invalid_signature"
    if "monitor" in message:
        return "monitoring_outage"
    if "tenant" in message or "isolation" in message:
        return "tenant_isolation_breach"
    if "audit" in message:
        return "audit_sink_unavailable"
    return "artifact_integrity_failure"


def _sha256(value: Any) -> str:
    from hindsight.v5_corpus import sha256_hex

    return sha256_hex(value)


def _signers() -> tuple[KmsHmacTokenizer, KmsHmacTokenizer]:
    key_id = (os.environ.get("HINDSIGHT_QUALIFICATION_HMAC_KEY_ID") or "").strip()
    if not key_id:
        raise RuntimeError("HINDSIGHT_QUALIFICATION_HMAC_KEY_ID is required")
    return (
        KmsHmacTokenizer(
            key_id=key_id,
            family_sha256=governance_v2_policy()["policy_sha256"],
        ),
        KmsHmacTokenizer(
            key_id=key_id,
            family_sha256=protected_study_protocol()["protocol_sha256"],
        ),
    )


def _gemini_embedding_provider() -> Any:
    from hindsight.embeddings import GeminiEmbeddingProvider
    from hindsight.gemini import gemini_pool_from_env
    from hindsight.runtime import runtime_settings
    from hindsight.v5_corpus import (
        EMBEDDING_DIMENSIONS,
        EMBEDDING_MODEL,
        GEMINI_PROVIDER_REPRESENTATION,
    )

    provider_env = {
        **os.environ,
        "LLM_PROVIDER": "gemini",
        "EMBEDDING_PROVIDER": "gemini",
        "GEMINI_EMBEDDING_MODEL": EMBEDDING_MODEL,
    }
    settings = runtime_settings(environ=provider_env, use_cache=False)
    return GeminiEmbeddingProvider(
        credential_pool=gemini_pool_from_env(settings.provider_env),
        model_name=EMBEDDING_MODEL,
        dimensions=EMBEDDING_DIMENSIONS,
        representation=GEMINI_PROVIDER_REPRESENTATION,
    )


def _database_urls() -> tuple[str, str]:
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    runtime_url = (os.environ.get("HINDSIGHT_V5_RUNTIME_DATABASE_URL") or "").strip()
    if not database_url or not runtime_url:
        raise RuntimeError("protected stages require deploy and restricted runtime database URLs")
    return database_url, runtime_url


def _exact_main_sha() -> str:
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status.strip():
        raise RuntimeError("v5 protected stages require a clean exact-main checkout")
    head = _git_sha("HEAD")
    main = _git_sha("origin/main")
    if head != main:
        raise RuntimeError("v5 protected stages require HEAD to equal origin/main")
    expected = os.environ.get("GITHUB_SHA")
    if expected and expected != head:
        raise RuntimeError("v5 protected checkout differs from GITHUB_SHA")
    return head


def _git_sha(revision: str) -> str:
    value = subprocess.run(
        ["git", "rev-parse", revision],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise RuntimeError(f"could not resolve exact Git identity for {revision}")
    return value


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain one JSON object")
    return value


def _write(path: pathlib.Path, value: dict[str, Any]) -> None:
    write_private_json_exclusive(path, value)


def _write_signed(path: pathlib.Path, value: dict[str, Any], *, stage: str) -> None:
    _write(path, value)
    _archive_value(
        key=(
            f"learning/v5/protected-studies/preparation/{stage}/"
            f"{value['artifact_sha256']}.json"
        ),
        value=value,
    )


def _progress(stage: str, current: int, total: int) -> None:
    interval = 10 if total <= 120 else 100
    if current == total or current % interval == 0:
        sys.stderr.write(f"v5 protected {stage}: {current}/{total}\n")
        sys.stderr.flush()


def _trial_progress(current: int, total: int) -> None:
    if current == total or current % 30 == 0:
        sys.stderr.write(f"v5 protected behavioral progress: {current}/{total}\n")
        sys.stderr.flush()


def _summary(value: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "status",
        "tested_subject_sha",
        "policy_evaluator_sha",
        "protected_runner_sha",
        "scenario_count",
        "selected_scenario_count",
        "trial_count",
        "checkpoint_sha256",
        "artifact_sha256",
        "corpus_sha256",
        "review_state_sha256",
    )
    return {field: value[field] for field in fields if field in value}


def _print_json(value: object) -> None:
    json.dump(value, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
