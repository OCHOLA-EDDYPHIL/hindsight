"""Synthetic quarantine redrive and cleanup acceptance contracts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from boto3.dynamodb.conditions import ConditionExpressionBuilder

from hindsight.quarantine import persist_quarantine_record
from tests.quarantine_fakes import InMemoryQuarantineTable

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = "a" * 40
SOURCE_RUN_ID = "11111111-1111-4111-8111-111111111111"
TARGET_RUN_ID = "22222222-2222-4222-8222-222222222222"
TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _load_script(name: str):
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


exercise = _load_script("exercise_quarantine_redrive")
cleanup = _load_script("cleanup_quarantine_acceptance")


class DeletingQuarantineTable(InMemoryQuarantineTable):
    def __init__(self) -> None:
        super().__init__()
        self.delete_calls = []

    def delete_item(self, *, Key, ConditionExpression):
        self.delete_calls.append({"Key": dict(Key), "ConditionExpression": ConditionExpression})
        self.items.pop(Key["quarantine_id"], None)
        return {}


class _Cursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, statement, parameters):
        assert "SELECT * FROM agent_runs WHERE id = %s" in statement
        assert parameters == (SOURCE_RUN_ID,)

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def cursor(self, *, row_factory):
        assert row_factory is not None
        return _Cursor(self.row)


def _persist(table):
    return persist_quarantine_record(
        table=table,
        source_arn="arn:aws:sqs:us-east-1:123456789012:hindsight-runs",
        source_message_id="7d28c8e5-00a6-4ef7-b0c0-75db7f9a7fc3",
        raw_body='{"command":"start","prompt":"must-not-leak"}',
        reason_code="run_attempts_exhausted",
        work_kind="run",
        command="start",
        receive_count=4,
        tenant_id=TENANT_ID,
        run_id=SOURCE_RUN_ID,
        command_generation=0,
        now=datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
    ).item


def _write_handoff(path: Path, record: dict[str, object]) -> None:
    payload = {
        "schema_version": 1,
        "source_revision": SOURCE_REVISION,
        "record": {key: record[key] for key in exercise.HANDOFF_RECORD_KEYS},
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    document = {**payload, "payload_sha256": hashlib.sha256(canonical).hexdigest()}
    path.write_text(json.dumps(document), encoding="utf-8")


def _exact_main(monkeypatch) -> None:
    environment = {
        "GITHUB_REPOSITORY": "owner/hindsight",
        "GITHUB_REPOSITORY_OWNER": "owner",
        "GITHUB_ACTOR": "owner",
        "GITHUB_TRIGGERING_ACTOR": "owner",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_PROTECTED": "true",
        "GITHUB_SHA": SOURCE_REVISION,
        "HINDSIGHT_ACCEPTANCE_SOURCE_REVISION": SOURCE_REVISION,
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)


def _mark_redriven(table, record, *, at: datetime) -> object:
    binding = exercise.quarantine_redrive_binding(
        quarantine_id=record["quarantine_id"],
        raw_body_sha256=record["raw_body_sha256"],
    )
    timestamp = at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    table.items[record["quarantine_id"]].update(
        {
            "status": "redriven",
            "redrive_effect_id": binding.effect_id,
            "redrive_binding_sha256": binding.binding_sha256,
            "redrive_started_at": timestamp,
            "redriven_at": timestamp,
            "redriven_run_id": TARGET_RUN_ID,
        }
    )
    return binding


def _effect_state(*, status: str, now: datetime, available_at: datetime) -> dict:
    copied = {
        "incident_slug": "checkout-latency",
        "namespace": "demo:payments",
        "user_input": "checkout latency",
        "service_slug": "payments-api",
        "retrieval_policy": "semantic_strict",
    }
    terminal = status == "rejected"
    return {
        "effect_count": 1,
        "source": {
            "id": SOURCE_RUN_ID,
            "status": "failed",
            "failure_code": "RunAttemptsExhausted",
            "worker_attempt_command": "start",
            "command_generation": 0,
            **copied,
        },
        "run": {
            "id": TARGET_RUN_ID,
            "decision_id": f"agent:{TARGET_RUN_ID}:plan",
            "status": status,
            "command_generation": 0,
            "worker_attempt_id": None,
            "worker_attempt_command": "start" if terminal else None,
            "worker_attempt_generation": 0 if terminal else None,
            "worker_attempt_lease_expires_at": None,
            "worker_attempt_count": 1 if terminal else 0,
            "completed_at": now if terminal else None,
            "model_call_count": 0,
            "cloudwatch_call_count": 0,
            "database_now": now,
            **copied,
        },
        "dispatch": {
            "id": "33333333-3333-4333-8333-333333333333",
            "status": "pending",
            "attempt_count": 0,
            "delivery_attempt_count": 0,
            "available_at": available_at,
            "lease_owner": None,
            "transport_message_id": None,
            "acknowledged_attempt_id": None,
            "dispatcher_run_eligible": not terminal,
        },
        "decision": {
            "status": "sealed" if terminal else "open",
            "sealed_at": now if terminal else None,
        },
    }


def test_redrive_exhausted_run_forwards_atomic_dispatch_fence(monkeypatch):
    import hindsight.runs as runs

    source = {
        "id": SOURCE_RUN_ID,
        "status": "failed",
        "failure_code": "RunAttemptsExhausted",
        "worker_attempt_command": "start",
        "command_generation": 0,
        "incident_slug": "checkout-latency",
        "namespace": "demo:payments",
        "user_input": "checkout latency",
        "service_slug": "payments-api",
        "retrieval_policy": "semantic_strict",
    }
    create_calls = []
    monkeypatch.setattr(
        runs,
        "connect",
        lambda db_url, *, application_name: _Connection(source),
    )
    monkeypatch.setattr(
        runs,
        "create_run",
        lambda **kwargs: create_calls.append(kwargs) or ({"id": TARGET_RUN_ID}, True),
    )
    fence = datetime(2036, 8, 11, 12, 0, tzinfo=UTC)

    result = runs.redrive_exhausted_run(
        run_id=SOURCE_RUN_ID,
        command="start",
        command_generation=0,
        idempotency_key="quarantine-redrive:effect",
        dispatch_available_at=fence,
        db_url="postgresql://db",
    )

    assert result == ({"id": TARGET_RUN_ID}, True)
    assert create_calls[0]["dispatch_available_at"] == fence


def test_exercise_redrives_once_then_rejects_without_external_work(monkeypatch, tmp_path):
    table = InMemoryQuarantineTable()
    record = _persist(table)
    handoff_path = tmp_path / "redrive-handoff.json"
    output_path = tmp_path / "redrive-evidence.json"
    _write_handoff(handoff_path, record)
    _exact_main(monkeypatch)
    now = datetime(2026, 8, 14, 12, 30, tzinfo=UTC)
    fence = now + exercise.FENCE_DURATION
    binding = exercise.quarantine_redrive_binding(
        quarantine_id=record["quarantine_id"],
        raw_body_sha256=record["raw_body_sha256"],
    )
    redrive_calls = []

    def redrive(**kwargs):
        redrive_calls.append(kwargs)
        if len(redrive_calls) == 1:
            _mark_redriven(table, record, at=now)
        return {
            "quarantine_id": binding.quarantine_id,
            "raw_body_sha256": binding.raw_body_sha256,
            "redrive_effect_id": binding.effect_id,
            "run_id": TARGET_RUN_ID,
            "status": "redriven",
            "created": len(redrive_calls) == 1,
        }

    states = iter(
        [
            _effect_state(status="queued", now=now, available_at=fence),
            _effect_state(status="rejected", now=now, available_at=fence),
        ]
    )
    claim_calls = []
    finish_calls = []
    monkeypatch.setattr(exercise, "redrive_quarantined_run", redrive)
    monkeypatch.setattr(exercise, "inspect_redrive_effect", lambda **_kwargs: next(states))
    monkeypatch.setattr(
        exercise,
        "claim_run_attempt",
        lambda **kwargs: (
            claim_calls.append(kwargs)
            or SimpleNamespace(outcome="claimed", attempt_id="acceptance-attempt")
        ),
    )
    monkeypatch.setattr(
        exercise,
        "finish_run_attempt",
        lambda **kwargs: finish_calls.append(kwargs),
    )

    evidence = exercise.exercise_quarantine_redrive(
        handoff_path=handoff_path,
        output_path=output_path,
        table=table,
        db_url="postgresql://db",
        now=now,
    )

    assert [call["dispatch_available_at"] for call in redrive_calls] == [fence, fence]
    assert all(call["confirmation"] == binding.confirmation for call in redrive_calls)
    assert all(call["expected_record_sha256"] == record["record_sha256"] for call in redrive_calls)
    assert claim_calls[0]["command"] == "start"
    assert finish_calls[0]["status"] == "rejected"
    assert finish_calls[0]["fields"] == {"action_approved": False}
    assert evidence["first_created"] is True
    assert evidence["repeat_created"] is False
    assert evidence["effect_count"] == 1
    assert evidence["run_status"] == "rejected"
    assert evidence["decision_status"] == "sealed"
    assert evidence["model_call_count"] == 0
    assert evidence["cloudwatch_call_count"] == 0
    assert evidence["dispatch_attempt_count"] == 0
    assert evidence["dispatch_delivery_attempt_count"] == 0
    assert evidence["dispatch_run_eligible"] is False
    payload = dict(evidence)
    supplied_digest = payload.pop("payload_sha256")
    assert supplied_digest == hashlib.sha256(exercise._canonical_bytes(payload)).hexdigest()
    serialized = output_path.read_text(encoding="utf-8")
    assert "must-not-leak" not in serialized
    assert "postgresql://" not in serialized


def test_exercise_rejects_mismatched_handoff_before_redrive(monkeypatch, tmp_path):
    table = InMemoryQuarantineTable()
    record = _persist(table)
    mismatched = dict(record)
    mismatched["run_id"] = "33333333-3333-4333-8333-333333333333"
    handoff_path = tmp_path / "redrive-handoff.json"
    _write_handoff(handoff_path, mismatched)
    _exact_main(monkeypatch)
    redrive_calls = []
    monkeypatch.setattr(
        exercise,
        "redrive_quarantined_run",
        lambda **kwargs: redrive_calls.append(kwargs),
    )

    try:
        exercise.exercise_quarantine_redrive(
            handoff_path=handoff_path,
            output_path=tmp_path / "redrive-evidence.json",
            table=table,
            db_url="postgresql://db",
            now=datetime(2026, 8, 14, 12, 30, tzinfo=UTC),
        )
    except exercise.QuarantineAcceptanceError as exc:
        assert "does not match the live record" in str(exc)
    else:
        raise AssertionError("mismatched handoff was accepted")

    assert redrive_calls == []
    stored = table.items[record["quarantine_id"]]
    assert stored["status"] == "quarantined"
    assert "redrive_effect_id" not in stored


def test_cleanup_recovers_partial_effect_and_deletes_only_exact_bound_item(
    monkeypatch,
    tmp_path,
):
    table = DeletingQuarantineTable()
    record = _persist(table)
    handoff_path = tmp_path / "redrive-handoff.json"
    output_path = tmp_path / "cleanup-evidence.json"
    repeat_output_path = tmp_path / "cleanup-repeat-evidence.json"
    _write_handoff(handoff_path, record)
    _exact_main(monkeypatch)
    now = datetime(2026, 8, 14, 12, 30, tzinfo=UTC)
    fence = now + exercise.FENCE_DURATION
    _mark_redriven(table, record, at=now)
    queued = _effect_state(status="queued", now=now, available_at=fence)
    terminal = _effect_state(status="rejected", now=now, available_at=fence)
    states = iter([queued, terminal, terminal])
    claim_calls = []
    finish_calls = []
    monkeypatch.setattr(cleanup, "find_redrive_effect", lambda **_kwargs: next(states))
    monkeypatch.setattr(
        cleanup,
        "claim_run_attempt",
        lambda **kwargs: (
            claim_calls.append(kwargs)
            or SimpleNamespace(outcome="claimed", attempt_id="cleanup-attempt")
        ),
    )
    monkeypatch.setattr(
        cleanup,
        "finish_run_attempt",
        lambda **kwargs: finish_calls.append(kwargs),
    )

    evidence = cleanup.cleanup_quarantine_acceptance(
        handoff_path=handoff_path,
        output_path=output_path,
        table=table,
        db_url="postgresql://db",
    )
    repeated = cleanup.cleanup_quarantine_acceptance(
        handoff_path=handoff_path,
        output_path=repeat_output_path,
        table=table,
        db_url="postgresql://db",
    )

    assert claim_calls[0]["command_generation"] == 0
    assert finish_calls[0]["status"] == "rejected"
    assert evidence["target_found"] is True
    assert evidence["effect_count"] == 1
    assert evidence["model_call_count"] == 0
    assert evidence["cloudwatch_call_count"] == 0
    assert evidence["dispatch_attempt_count"] == 0
    assert evidence["dispatch_delivery_attempt_count"] == 0
    assert evidence["dispatch_run_eligible"] is False
    assert evidence["ledger_deleted"] is True
    assert evidence["ledger_absent"] is True
    assert repeated["ledger_deleted"] is False
    assert repeated["ledger_absent"] is True
    assert table.items == {}
    assert len(table.delete_calls) == 1
    built = ConditionExpressionBuilder().build_expression(
        table.delete_calls[0]["ConditionExpression"]
    )
    assert cleanup.STORED_RECORD_KEYS == set(built.attribute_name_placeholders.values())
    assert "attribute_not_exists" in built.condition_expression
    payload = dict(evidence)
    supplied_digest = payload.pop("payload_sha256")
    assert supplied_digest == hashlib.sha256(exercise._canonical_bytes(payload)).hexdigest()
