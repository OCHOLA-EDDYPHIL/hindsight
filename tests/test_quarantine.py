"""Terminal worker quarantine and redrive boundaries."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from boto3.dynamodb.conditions import ConditionExpressionBuilder

from tests.quarantine_fakes import (
    ConditionalCheckFailedException,
    InMemoryQuarantineTable,
    QueryingQuarantineTable,
)

SOURCE_ARN = "arn:aws:sqs:us-east-1:123456789012:hindsight-runs"
MESSAGE_ID = "7d28c8e5-00a6-4ef7-b0c0-75db7f9a7fc3"
TENANT_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "11111111-1111-4111-8111-111111111111"
RACED_RUN_ID = "33333333-3333-4333-8333-333333333333"
QUARANTINE_CORE_KEYS = frozenset(
    {
        "schema_version",
        "quarantine_id",
        "source_arn",
        "source_message_id",
        "raw_body_sha256",
        "reason_code",
        "work_kind",
        "command",
        "receive_count",
        "tenant_id",
        "run_id",
        "operation_id",
        "command_generation",
    }
)


class RacingQuarantineTable(InMemoryQuarantineTable):
    def __init__(self, *, race_phase: str) -> None:
        super().__init__()
        self.race_phase = race_phase
        self.raced = False
        self.raced_condition = None

    def update_item(self, **kwargs):
        phase = "claim" if ":pending" in kwargs["ExpressionAttributeValues"] else "completion"
        if phase == self.race_phase and not self.raced:
            self.raced = True
            self.raced_condition = kwargs["ConditionExpression"]
            _replace_stored_run_identity(
                self.items[kwargs["Key"]["quarantine_id"]],
                run_id=RACED_RUN_ID,
            )
            raise ConditionalCheckFailedException()
        return super().update_item(**kwargs)


def _replace_stored_run_identity(item: dict, *, run_id: str) -> None:
    item["run_id"] = run_id
    core = {key: item[key] for key in QUARANTINE_CORE_KEYS if key in item}
    canonical = json.dumps(
        core,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    item["record_sha256"] = hashlib.sha256(canonical).hexdigest()


def _condition_attributes(condition) -> set[str]:
    built = ConditionExpressionBuilder().build_expression(condition)
    return set(built.attribute_name_placeholders.values())


def _persist(table, *, raw_body='{"command":"start"}', receive_count=4, now=None):
    from hindsight.quarantine import persist_quarantine_record

    return persist_quarantine_record(
        table=table,
        source_arn=SOURCE_ARN,
        source_message_id=MESSAGE_ID,
        raw_body=raw_body,
        reason_code="run_attempts_exhausted",
        work_kind="run",
        command="start",
        receive_count=receive_count,
        tenant_id=TENANT_ID,
        run_id=RUN_ID,
        command_generation=0,
        now=now or datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
    )


def test_quarantine_stores_only_allowlisted_identity_and_exact_body_digest():
    from hindsight.quarantine import stable_quarantine_id

    table = InMemoryQuarantineTable()
    raw_body = (
        '{"command":"start","prompt":"delete production",'
        '"token":"opaque-sensitive-material-must-not-persist"}'
    )

    write = _persist(table, raw_body=raw_body)

    assert write.created is True
    assert write.item["quarantine_id"] == stable_quarantine_id(
        source_arn=SOURCE_ARN,
        source_message_id=MESSAGE_ID,
    )
    assert write.item["raw_body_sha256"] == hashlib.sha256(raw_body.encode()).hexdigest()
    assert set(write.item) == {
        "schema_version",
        "quarantine_id",
        "source_arn",
        "source_message_id",
        "raw_body_sha256",
        "reason_code",
        "work_kind",
        "command",
        "receive_count",
        "tenant_id",
        "run_id",
        "command_generation",
        "status",
        "created_at",
        "record_sha256",
    }
    serialized = repr(write.item)
    assert "delete production" not in serialized
    assert "opaque-sensitive-material" not in serialized


def test_quarantine_identical_retry_is_idempotent_and_conflict_is_rejected():
    from hindsight.quarantine import QuarantineConflictError

    table = InMemoryQuarantineTable()
    first = _persist(table)
    repeated = _persist(
        table,
        receive_count=5,
        now=datetime(2026, 8, 14, 13, 0, tzinfo=UTC),
    )

    assert first.created is True
    assert repeated.created is False
    assert repeated.item["created_at"] == first.item["created_at"]
    with pytest.raises(QuarantineConflictError, match="different terminal work"):
        _persist(table, raw_body='{"command":"resume"}')


def test_quarantine_rejects_run_identity_for_untrusted_malformed_work():
    from hindsight.quarantine import QuarantineRecordError, persist_quarantine_record

    with pytest.raises(QuarantineRecordError, match="non-run quarantine"):
        persist_quarantine_record(
            table=InMemoryQuarantineTable(),
            source_arn=SOURCE_ARN,
            source_message_id=MESSAGE_ID,
            raw_body="not-json",
            reason_code="malformed_json",
            work_kind="unknown",
            command="unsupported",
            run_id=RUN_ID,
        )


def test_quarantine_metric_report_counts_pending_work_and_oldest_age():
    from hindsight.quarantine import report_quarantine_metrics

    table = QueryingQuarantineTable(
        {
            "quarantined": [
                [
                    {"quarantine_id": "q_1", "created_at": "2026-08-14T11:00:00Z"},
                    {"quarantine_id": "q_2", "created_at": "2026-08-14T11:30:00Z"},
                ],
                [{"quarantine_id": "q_3", "created_at": "2026-08-14T11:45:00Z"}],
            ],
            "redrive_pending": [[{"quarantine_id": "q_4", "created_at": "2026-08-14T10:00:00Z"}]],
        }
    )
    cloudwatch = SimpleNamespace(calls=[])
    cloudwatch.put_metric_data = lambda **kwargs: cloudwatch.calls.append(kwargs)

    result = report_quarantine_metrics(
        table=table,
        cloudwatch_client=cloudwatch,
        stage="demo",
        now=datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
    )

    assert result == {"count": 4, "oldest_age_seconds": 7200}
    assert cloudwatch.calls == [
        {
            "Namespace": "Hindsight/Quarantine",
            "MetricData": [
                {
                    "MetricName": "QuarantineRecordCount",
                    "Dimensions": [{"Name": "Stage", "Value": "demo"}],
                    "Value": 4,
                    "Unit": "Count",
                },
                {
                    "MetricName": "OldestRecordAgeSeconds",
                    "Dimensions": [{"Name": "Stage", "Value": "demo"}],
                    "Value": 7200,
                    "Unit": "Seconds",
                },
            ],
        }
    ]


def test_dynamodb_decimal_numbers_validate_as_canonical_integers():
    from hindsight.quarantine import validate_stored_quarantine_item

    item = _persist(InMemoryQuarantineTable()).item
    item["schema_version"] = Decimal("1")
    item["receive_count"] = Decimal("4")
    item["command_generation"] = Decimal("0")

    validated = validate_stored_quarantine_item(item)

    assert validated["schema_version"] == 1
    assert validated["receive_count"] == 4
    assert validated["command_generation"] == 0
    assert all(
        type(validated[key]) is int
        for key in ("schema_version", "receive_count", "command_generation")
    )


def test_duplicate_read_accepts_dynamodb_decimal_numbers():
    table = InMemoryQuarantineTable()
    first = _persist(table)
    stored = table.items[first.item["quarantine_id"]]
    stored["schema_version"] = Decimal("1")
    stored["receive_count"] = Decimal("4")
    stored["command_generation"] = Decimal("0")

    repeated = _persist(table, receive_count=5)

    assert repeated.created is False
    assert repeated.item["receive_count"] == 4
    assert type(repeated.item["receive_count"]) is int


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("receive_count", Decimal("4.5")),
        ("command_generation", Decimal("-1")),
        ("schema_version", Decimal("2")),
        ("schema_version", True),
        ("receive_count", True),
        ("receive_count", 2**63),
        ("command_generation", 2**63),
    ],
)
def test_stored_numeric_fields_reject_noncanonical_values(key, value):
    from hindsight.quarantine import QuarantineRecordError, validate_stored_quarantine_item

    item = _persist(InMemoryQuarantineTable()).item
    item[key] = value

    with pytest.raises(QuarantineRecordError):
        validate_stored_quarantine_item(item)


def test_owner_gated_redrive_has_one_logical_effect(monkeypatch):
    import hindsight.quarantine_redrive as redrive

    table = InMemoryQuarantineTable()
    item = _persist(table).item
    stored = table.items[item["quarantine_id"]]
    stored["schema_version"] = Decimal("1")
    stored["receive_count"] = Decimal("4")
    stored["command_generation"] = Decimal("0")
    source = {
        "id": RUN_ID,
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
    created_run = {"id": "22222222-2222-4222-8222-222222222222"}
    create_calls = []

    def get_run(*, run_id, db_url):
        assert db_url == "postgresql://db"
        return source if run_id == RUN_ID else created_run

    def create_run(**kwargs):
        create_calls.append(kwargs)
        return created_run, True

    monkeypatch.setattr(redrive, "get_run", get_run)
    monkeypatch.setattr(redrive, "redrive_exhausted_run", create_run)
    confirmation = f"redrive:{item['quarantine_id']}:{item['raw_body_sha256']}"
    dispatch_available_at = datetime(2026, 8, 14, 12, 30, tzinfo=UTC) + timedelta(days=3650)
    common = {
        "table": table,
        "quarantine_id": item["quarantine_id"],
        "raw_body_sha256": item["raw_body_sha256"],
        "confirmation": confirmation,
        "repository_owner": "owner",
        "actor": "owner",
        "triggering_actor": "owner",
        "db_url": "postgresql://db",
        "dispatch_available_at": dispatch_available_at,
        "now": datetime(2026, 8, 14, 12, 30, tzinfo=UTC),
    }

    first = redrive.redrive_quarantined_run(**common)
    repeated = redrive.redrive_quarantined_run(**common)

    assert first["status"] == "redriven"
    assert first["created"] is True
    assert repeated == {**first, "created": False}
    assert len(create_calls) == 1
    assert create_calls[0]["idempotency_key"].startswith("quarantine-redrive:")
    assert create_calls[0]["dispatch_available_at"] == dispatch_available_at
    assert repeated["redrive_effect_id"] == first["redrive_effect_id"]


def test_redrive_claim_rejects_a_core_record_race(monkeypatch):
    import hindsight.quarantine_redrive as redrive

    table = RacingQuarantineTable(race_phase="claim")
    item = _persist(table).item
    create_calls = []
    monkeypatch.setattr(
        redrive,
        "redrive_exhausted_run",
        lambda **kwargs: create_calls.append(kwargs),
    )

    with pytest.raises(redrive.QuarantineRedriveError, match="bound to another redrive"):
        redrive.redrive_quarantined_run(
            table=table,
            quarantine_id=item["quarantine_id"],
            raw_body_sha256=item["raw_body_sha256"],
            confirmation=f"redrive:{item['quarantine_id']}:{item['raw_body_sha256']}",
            repository_owner="owner",
            actor="owner",
            triggering_actor="owner",
            db_url="postgresql://db",
            expected_record_sha256=item["record_sha256"],
        )

    assert create_calls == []
    assert table.items[item["quarantine_id"]]["status"] == "quarantined"
    assert table.items[item["quarantine_id"]]["run_id"] == RACED_RUN_ID
    assert {"status", "raw_body_sha256", "record_sha256"} <= _condition_attributes(
        table.raced_condition
    )


def test_redrive_completion_rejects_a_core_record_race(monkeypatch):
    import hindsight.quarantine_redrive as redrive
    from hindsight.quarantine_redrive import QuarantineRedriveError

    table = RacingQuarantineTable(race_phase="completion")
    item = _persist(table).item
    created_run = {"id": "22222222-2222-4222-8222-222222222222"}
    create_calls = []
    monkeypatch.setattr(
        redrive,
        "redrive_exhausted_run",
        lambda **kwargs: create_calls.append(kwargs) or (created_run, True),
    )

    with pytest.raises(QuarantineRedriveError, match="completion conflicted"):
        redrive.redrive_quarantined_run(
            table=table,
            quarantine_id=item["quarantine_id"],
            raw_body_sha256=item["raw_body_sha256"],
            confirmation=f"redrive:{item['quarantine_id']}:{item['raw_body_sha256']}",
            repository_owner="owner",
            actor="owner",
            triggering_actor="owner",
            db_url="postgresql://db",
            expected_record_sha256=item["record_sha256"],
        )

    stored = table.items[item["quarantine_id"]]
    assert len(create_calls) == 1
    assert stored["status"] == "redrive_pending"
    assert stored["run_id"] == RACED_RUN_ID
    assert "redriven_at" not in stored
    assert "redriven_run_id" not in stored
    assert {
        "status",
        "redrive_effect_id",
        "redrive_binding_sha256",
        "raw_body_sha256",
        "record_sha256",
    } <= _condition_attributes(table.raced_condition)


def test_redrive_rejects_naive_dispatch_fence_before_mutating_ledger():
    from hindsight.quarantine_redrive import QuarantineRedriveError, redrive_quarantined_run

    table = InMemoryQuarantineTable()
    item = _persist(table).item

    with pytest.raises(QuarantineRedriveError, match="timezone"):
        redrive_quarantined_run(
            table=table,
            quarantine_id=item["quarantine_id"],
            raw_body_sha256=item["raw_body_sha256"],
            confirmation=f"redrive:{item['quarantine_id']}:{item['raw_body_sha256']}",
            repository_owner="owner",
            actor="owner",
            triggering_actor="owner",
            db_url="postgresql://db",
            dispatch_available_at=datetime(2036, 8, 14),
        )

    assert table.items[item["quarantine_id"]]["status"] == "quarantined"


def test_redrive_rejects_an_expected_record_digest_mismatch_before_claim():
    from hindsight.quarantine_redrive import QuarantineRedriveError, redrive_quarantined_run

    table = InMemoryQuarantineTable()
    item = _persist(table).item

    with pytest.raises(QuarantineRedriveError, match="record digest"):
        redrive_quarantined_run(
            table=table,
            quarantine_id=item["quarantine_id"],
            raw_body_sha256=item["raw_body_sha256"],
            confirmation=f"redrive:{item['quarantine_id']}:{item['raw_body_sha256']}",
            repository_owner="owner",
            actor="owner",
            triggering_actor="owner",
            db_url="postgresql://db",
            expected_record_sha256="f" * 64,
        )

    stored = table.items[item["quarantine_id"]]
    assert stored["status"] == "quarantined"
    assert "redrive_effect_id" not in stored


@pytest.mark.parametrize(
    ("actor", "triggering_actor"),
    [("attacker", "owner"), ("owner", "attacker")],
)
def test_redrive_requires_both_owner_identities(actor, triggering_actor):
    from hindsight.quarantine_redrive import QuarantineRedriveError, redrive_quarantined_run

    with pytest.raises(QuarantineRedriveError, match="repository owner"):
        redrive_quarantined_run(
            table=InMemoryQuarantineTable(),
            quarantine_id="q_" + "0" * 64,
            raw_body_sha256="0" * 64,
            confirmation="redrive:q_" + "0" * 64 + ":" + "0" * 64,
            repository_owner="owner",
            actor=actor,
            triggering_actor=triggering_actor,
            db_url="postgresql://db",
        )
