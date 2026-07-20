from __future__ import annotations

import pathlib
import os
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from hindsight import learning_authority, learning_result


ROOT = pathlib.Path(__file__).resolve().parents[1]
requires_db = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not set")


class _Archive:
    bucket = "evidence-bucket"

    def __init__(self, *, previous_class: str = "infrastructure_outcome_free"):
        self.previous_class = previous_class
        self.writes = []

    def get_canonical_json_if_exists(self, *, key):
        assert key == learning_authority.execution_key(1, "finalization")
        return (
            {"terminal_class": self.previous_class},
            {"sha256": "f" * 64},
        )

    def put_canonical_json(self, *, key, payload):
        self.writes.append((key, payload))
        return {
            "key": key,
            "version_id": "version-1",
            "sha256": learning_authority.canonical_sha256(payload),
        }


def _execution_payload(sequence: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "protocol_authorization_id": learning_authority.protocol_authorization_id(),
        "protocol_authorization_sha256": "a" * 64,
        "execution_authorization_id": learning_authority.execution_authorization_id(
            sequence
        ),
        "sequence": sequence,
        "authorized_by": "owner",
        "authorization_workflow_run_id": 12,
        "authorization_workflow_run_attempt": 1,
        "previous_finalization_sha256": "f" * 64,
    }


def test_sequence_two_requires_outcome_free_infrastructure_terminal(monkeypatch):
    monkeypatch.setattr(
        learning_authority,
        "_load_protocol",
        lambda **_kwargs: {"record": {"sha256": "a" * 64}},
    )
    monkeypatch.setattr(
        learning_authority,
        "_mirror_execution_authorization",
        lambda **_kwargs: None,
    )
    archive = _Archive()

    accepted = learning_authority.authorize_execution(
        archive=archive,
        db_url="postgresql://unused",
        sequence=2,
        payload=_execution_payload(2),
    )

    assert accepted["record"]["key"].endswith("execution-2/authorization.json")
    archive.previous_class = "infrastructure_outcome_bearing"
    with pytest.raises(RuntimeError, match="outcome-free"):
        learning_authority.authorize_execution(
            archive=archive,
            db_url="postgresql://unused",
            sequence=2,
            payload=_execution_payload(2),
        )


def test_result_classifier_keeps_science_separate_from_workflow_success(monkeypatch):
    completed = {
        "id": "confirmation-id",
        "experiment_kind": "confirmation",
        "status": "completed",
        "scientific_failure": False,
        "outcome_bearing": True,
    }
    gates = {gate: True for gate in learning_result._PROTOCOL_GATES}
    gates.update({"efficacy": False, "reference_noninferiority": True, "safety": True})
    monkeypatch.setattr(
        learning_result,
        "benchmark_report",
        lambda **_kwargs: {"gates": gates, "claim_authorized": False},
    )

    classified = learning_result._classify(
        experiments=[completed],
        db_url="postgresql://unused",
    )

    assert classified["result"] == "not_demonstrated"
    assert classified["protocol_valid"] is True
    assert classified["terminal_class"] == "not_demonstrated"


def test_incomplete_confirmation_controls_replacement_eligibility():
    base = {
        "id": "confirmation-id",
        "experiment_kind": "confirmation",
        "status": "incomplete",
        "scientific_failure": False,
    }
    outcome_free = learning_result._classify(
        experiments=[{**base, "outcome_bearing": False}],
        db_url="postgresql://unused",
    )
    outcome_bearing = learning_result._classify(
        experiments=[{**base, "outcome_bearing": True}],
        db_url="postgresql://unused",
    )

    assert outcome_free["terminal_class"] == "infrastructure_outcome_free"
    assert outcome_bearing["terminal_class"] == "infrastructure_outcome_bearing"
    assert outcome_free["result"] == outcome_bearing["result"] == "inconclusive"


def test_learning_workflow_serializes_authority_science_finalization_and_seal():
    workflow_path = ROOT / ".github" / "workflows" / "learning-evidence.yml"
    workflow = workflow_path.read_text()
    parsed = yaml.safe_load(workflow)

    assert set(parsed["jobs"]) == {
        "authorize",
        "exact_main_ci",
        "claim",
        "pilot",
        "preregister",
        "confirmation",
        "finalize",
        "seal",
        "learning_evidence_complete",
    }
    assert "protocol-v3-reset-1" not in workflow
    assert "reconcile-consumption" in workflow
    assert "seal-only" in workflow
    assert "configure_changefeed.py" not in workflow
    assert "HINDSIGHT_LEARNING_PROTOCOL_RESET_ID" not in workflow
    assert workflow.index("  pilot:") < workflow.index("  preregister:")
    assert workflow.index("  preregister:") < workflow.index("  confirmation:")
    assert workflow.index("  confirmation:") < workflow.index("  finalize:")
    assert workflow.index("  finalize:") < workflow.index("  seal:")


def test_execution_evidence_migration_allows_only_one_scientific_terminal():
    migration = (ROOT / "migrations" / "0024_learning_execution_evidence.sql").read_text()

    assert "DROP INDEX IF EXISTS learning_evidence_consumed_reset_idx" in migration
    assert "learning_evidence_execution_study_idx" in migration
    assert "learning_evidence_scientific_reset_idx" in migration
    assert "result IN ('accepted', 'not_demonstrated')" in migration


@requires_db
def test_archive_authority_reconciles_consumes_and_finalizes_in_database():
    from hindsight.db import connect, database_url
    from hindsight.server_tenants import learning_tenant_id
    from hindsight.tenant import tenant_scope

    class Archive:
        bucket = "evidence-bucket"

        def __init__(self):
            self.objects = {}

        def put_canonical_json(self, *, key, payload):
            record = {
                "key": key,
                "version_id": f"version-{len(self.objects) + 1}",
                "sha256": learning_authority.canonical_sha256(payload),
                "retain_until": (datetime.now(UTC) + timedelta(days=2557)).isoformat(),
            }
            existing = self.objects.get(key)
            if existing is not None and existing[0] != payload:
                raise RuntimeError("different content")
            if existing is None:
                self.objects[key] = (payload, record)
            return self.objects[key][1]

        def get_canonical_json(self, *, key, version_id=None):
            payload, record = self.objects[key]
            assert version_id is None or version_id == record["version_id"]
            return payload, record

        def get_canonical_json_if_exists(self, *, key):
            return self.objects.get(key)

        def seal_bundle(self, *, evidence_id, objects, dependencies):
            report = self.put_canonical_json(
                key=f"learning/evidence/{evidence_id}/study.json",
                payload=objects["study"],
            )
            manifest = self.put_canonical_json(
                key=f"learning/evidence/{evidence_id}/manifest.json",
                payload={"objects": {"study": report}, "dependencies": dependencies},
            )
            return {
                "schema_version": 1,
                "evidence_id": evidence_id,
                "bucket": self.bucket,
                "manifest_key": manifest["key"],
                "manifest_version_id": manifest["version_id"],
                "manifest_sha256": manifest["sha256"],
                "retain_until": manifest["retain_until"],
            }

    archive = Archive()
    with tenant_scope(learning_tenant_id()):
        with connect(database_url()) as conn:
            if conn.execute(
                "SELECT 1 FROM learning_protocol_authorizations WHERE authorization_slot = %s",
                (learning_authority.PROTOCOL_SLOT,),
            ).fetchone():
                pytest.skip("the one fixed protocol authority is already populated")
    protocol = {
        "schema_version": 1,
        "authorization_slot": learning_authority.PROTOCOL_SLOT,
        "protocol_authorization_id": learning_authority.protocol_authorization_id(),
        "protocol_schema_version": 3,
        "protocol_identity_sha256": "protocol-db-test",
        "corpus_sha256": "corpus-db-test",
        "code_sha": "a" * 40,
        "reasoning_provider": "gemini",
        "reasoning_model": "gemini-3.1-flash-lite",
        "embedding_profile_id": "profile-db-test",
        "embedding_provider": "gemini",
        "embedding_model": "gemini-embedding-2",
        "embedding_max_distance": 0.35,
        "qualification_run_id": 101,
        "qualification_evidence_sha256": "qualification-db-test",
        "product_run_id": 102,
        "product_provenance_sha256": "product-db-test",
        "product_provenance_archive_key": learning_authority.PRODUCT_PROVENANCE_KEY,
        "product_provenance_archive_version_id": "product-version-db-test",
        "authorized_by": "owner",
        "authorization_workflow_run_id": 103,
        "authorization_workflow_run_attempt": 1,
    }
    learning_authority.authorize_protocol(
        archive=archive,
        db_url=database_url(),
        payload=protocol,
    )
    protocol_digest = learning_authority.canonical_sha256(protocol)
    execution = _execution_payload(1)
    execution["protocol_authorization_sha256"] = protocol_digest
    execution.pop("previous_finalization_sha256")
    learning_authority.authorize_execution(
        archive=archive,
        db_url=database_url(),
        sequence=1,
        payload=execution,
    )
    learning_authority.consume_execution(
        archive=archive,
        db_url=database_url(),
        sequence=1,
        workflow_run_id=103,
        workflow_run_attempt=1,
        code_sha="a" * 40,
    )
    study = {
        "schema_version": 1,
        "sequence": 1,
        "execution_authorization_id": learning_authority.execution_authorization_id(1),
        "workflow_run_id": 103,
        "workflow_run_attempt": 1,
        "code_sha": "a" * 40,
        "result": "inconclusive",
        "protocol_valid": True,
        "reason_code": "infrastructure_before_confirmation",
        "terminal_class": "infrastructure_outcome_free",
        "terminal_reason": "Infrastructure ended before confirmation.",
        "protocol_identity_sha256": "protocol-db-test",
    }
    learning_authority.seal_execution(
        archive=archive,
        db_url=database_url(),
        sequence=1,
        study_report=study,
    )

    with tenant_scope(learning_tenant_id()):
        with connect(database_url()) as conn:
            execution_row = conn.execute(
                "SELECT status, terminal_class FROM learning_execution_authorizations WHERE id = %s",
                (learning_authority.execution_authorization_id(1),),
            ).fetchone()
            evidence_row = conn.execute(
                "SELECT result, protocol_valid FROM learning_evidence_records WHERE execution_authorization_id = %s",
                (learning_authority.execution_authorization_id(1),),
            ).fetchone()
    assert execution_row == ("finalized", "infrastructure_outcome_free")
    assert evidence_row == ("inconclusive", True)
