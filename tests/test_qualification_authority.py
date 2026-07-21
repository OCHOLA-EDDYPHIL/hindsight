from __future__ import annotations

import hashlib
from pathlib import Path
from copy import deepcopy

import pytest

from hindsight import qualification_authority
from hindsight.evidence_archive import canonical_json_bytes
from hindsight.opaque_tokens import KmsHmacTokenizer


class _Archive:
    bucket = "evidence-bucket"

    def __init__(self):
        self.objects = {}

    def put_canonical_json(self, *, key, payload):
        record = {
            "key": key,
            "version_id": f"version-{len(self.objects) + 1}",
            "sha256": hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        }
        existing = self.objects.get(key)
        if existing is not None and existing[0] != payload:
            raise RuntimeError("different content")
        if existing is None:
            self.objects[key] = (deepcopy(payload), record)
        return self.objects[key][1]

    def get_canonical_json(self, *, key, version_id=None):
        payload, record = self.objects[key]
        assert version_id is None or version_id == record["version_id"]
        return deepcopy(payload), record

    def get_canonical_json_if_exists(self, *, key):
        value = self.objects.get(key)
        if value is None:
            return None
        return deepcopy(value[0]), value[1]


def _contract() -> dict[str, object]:
    return qualification_authority.v3_family_contract(corpus_sha256="a" * 64)


def test_family_identity_excludes_code_and_workflow_identity():
    contract = _contract()
    digest = qualification_authority.family_sha256(contract)

    assert digest == qualification_authority.family_sha256(deepcopy(contract))
    assert "code_sha" not in canonical_json_bytes(contract).decode()
    assert "workflow" not in canonical_json_bytes(contract).decode()


def test_terminal_family_rejects_new_claim_before_attempt_creation():
    archive = _Archive()
    contract = _contract()
    digest = qualification_authority.family_sha256(contract)
    archive.put_canonical_json(
        key=qualification_authority.terminal_key(digest),
        payload={"terminal_class": "scientific_failed"},
    )

    with pytest.raises(RuntimeError, match="terminal"):
        qualification_authority.claim_attempt(
            archive=archive,
            contract=contract,
            sequence=1,
            actor="owner",
            workflow_run_id=7,
            workflow_run_attempt=1,
            code_sha="b" * 40,
        )

    assert len(archive.objects) == 1


def test_preserved_v3_scientific_failure_is_terminal_without_archive_repair():
    archive = _Archive()
    contract = qualification_authority.v3_family_contract(
        corpus_sha256="db121ca2071a2af03bcd7097fa472f4d8d1a7051474f9cb0aa91364742073d4a"
    )

    assert (
        qualification_authority.family_sha256(contract)
        == qualification_authority.V3_TERMINAL_FAMILY_SHA256
    )
    with pytest.raises(RuntimeError, match="terminal"):
        qualification_authority.claim_attempt(
            archive=archive,
            contract=contract,
            sequence=1,
            actor="owner",
            workflow_run_id=7,
            workflow_run_attempt=1,
            code_sha="b" * 40,
        )

    assert archive.objects == {}


def test_rerun_identity_and_sequence_two_are_fail_closed():
    archive = _Archive()
    contract = _contract()
    first = qualification_authority.claim_attempt(
        archive=archive,
        contract=contract,
        sequence=1,
        actor="owner",
        workflow_run_id=7,
        workflow_run_attempt=1,
        code_sha="b" * 40,
    )
    assert first["consumption"]["sha256"]

    with pytest.raises(RuntimeError, match="different content"):
        qualification_authority.claim_attempt(
            archive=archive,
            contract=contract,
            sequence=1,
            actor="owner",
            workflow_run_id=7,
            workflow_run_attempt=2,
            code_sha="b" * 40,
        )
    with pytest.raises(RuntimeError, match="sequence-one finalization"):
        qualification_authority.claim_attempt(
            archive=archive,
            contract=contract,
            sequence=2,
            actor="owner",
            workflow_run_id=8,
            workflow_run_attempt=1,
            code_sha="b" * 40,
        )


def test_sequence_two_requires_outcome_free_finalization():
    archive = _Archive()
    contract = _contract()
    digest = qualification_authority.family_sha256(contract)
    archive.put_canonical_json(
        key=qualification_authority.attempt_key(
            family_digest=digest,
            sequence=1,
            name="finalization",
        ),
        payload={"terminal_class": "infrastructure_outcome_free"},
    )

    claimed = qualification_authority.claim_attempt(
        archive=archive,
        contract=contract,
        sequence=2,
        actor="owner",
        workflow_run_id=8,
        workflow_run_attempt=1,
        code_sha="b" * 40,
    )

    assert claimed["attempt_id"] == qualification_authority.attempt_id(
        family_digest=digest,
        sequence=2,
    )


def test_kms_tokens_are_domain_separated_and_fixed_length():
    class Kms:
        def __init__(self):
            self.messages = []

        def generate_mac(self, **kwargs):
            self.messages.append(kwargs)
            return {"Mac": hashlib.sha256(bytes(kwargs["Message"])).digest()}

    client = Kms()
    tokenizer = KmsHmacTokenizer(
        key_id="alias/qualification-hmac",
        family_sha256="f" * 64,
        client=client,
    )

    target = tokenizer.token(kind="target", raw_id="item-1")
    candidate = tokenizer.token(kind="candidate", raw_id="item-1")

    assert len(target) == 64
    assert target != candidate
    assert all(call["MacAlgorithm"] == "HMAC_SHA_256" for call in client.messages)
    assert all(call["KeyId"] == "alias/qualification-hmac" for call in client.messages)


def test_outcome_access_makes_infrastructure_interruption_terminal():
    counts = {"benchmark_experiments": 0}
    report = {
        "status": "infrastructure_incomplete",
        "summary": {"completed_variants": 0},
        "benchmark_row_counts_before": counts,
        "benchmark_row_counts_after": counts,
        "outcome_accessed": False,
    }

    assert qualification_authority._terminal_class(report=report, sequence=1) == (
        "infrastructure_outcome_free",
        False,
    )
    report["outcome_accessed"] = True
    assert qualification_authority._terminal_class(report=report, sequence=1) == (
        "infrastructure_outcome_bearing",
        True,
    )


def test_qualification_family_migration_is_tenant_bound_and_guarded():
    migration = (
        qualification_authority.__file__
    )  # keep module import tied to the installed source tree
    assert migration
    sql = Path("migrations/0025_learning_qualification_families.sql").read_text(encoding="utf-8")
    assert "learning_qualification_attempts" in sql
    assert "learning_qualification_family_terminals" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "learning_qualification_attempt_guarded" in sql
    assert "learning_qualification_terminal_immutable" in sql
