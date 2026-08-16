from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE_REVISION = "a" * 40


def _script(name: str):
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retrieval_report_preserves_raw_measurements_and_limitations():
    module = _script("evaluate_retrieval_benchmark")
    fixture = json.loads((ROOT / "fixtures/retrieval_incidents.json").read_text())
    results = {
        "environment": {"database": "isolated"},
        "cases": [
            {"query_id": row["query_id"], "memory_ids": row["relevant_memory_ids"]}
            for row in fixture["cases"]
        ],
    }
    report = module.evaluate(fixture, results, source_revision=SOURCE_REVISION)
    assert report["measurements"]["recall_at_k"] == 1.0
    assert report["measurements"]["mrr"] == 1.0
    assert report["raw_measurements"]
    assert "not a production SLO" in report["limitations"][1]


def test_capacity_evidence_requires_bound_supplemental_artifacts():
    module = _script("validate_capacity_evidence")
    evidence = {
        "schema_version": module.SCHEMA_VERSION,
        "source_revision": SOURCE_REVISION,
        "index_qualification": {
            "qualified": False,
            "artifact_sha256": "b" * 64,
            "main_sha": SOURCE_REVISION,
        },
        "targets": module.TARGETS,
        "ceilings": module.EXPECTED_CEILINGS,
        "method": {"duration_seconds": 60},
        "environment": {
            "isolation": "run_scoped_database_and_compose_project",
            "paid_model_calls": 0,
            "live_worker_invocations": 0,
        },
        "raw_measurements": [
            {"name": "vector_seed", "duration_seconds": 1},
            {
                "name": "vector_counts",
                "total": 100_000,
                "per_tenant": [
                    {"tenant_id": f"tenant-{number}", "vectors": 5_000} for number in range(20)
                ],
            },
            {
                "name": "bounded_clients",
                "clients": [
                    {
                        "client": number,
                        "qualified_index": module.EXPECTED_INDEX,
                        "prefix_spans": "[/tenant - /tenant]",
                        "plan": f"vector search table: semantic_memory_vectors@{module.EXPECTED_INDEX}",
                    }
                    for number in range(1, 21)
                ],
            },
            {
                "name": "synthetic_backlog",
                "messages_enqueued": 1_000,
                "messages_accounted_for": 1_000,
                "clients": 20,
                "per_client_counts": [50] * 20,
                "live_worker_invocations": 0,
                "paid_model_calls": 0,
            },
            {"name": "storage", "bytes": 1_000_000},
            {"name": "total", "duration_seconds": 10},
        ],
        "limitations": ["Bounded benchmark evidence; not production SLO claims."],
    }
    with pytest.raises(ValueError, match="requires qualification, cleanup, and artifact"):
        module.validate(
            evidence,
            source_revision=SOURCE_REVISION,
            execution_id="capacity_123_1_qualification",
        )


def test_alert_exercise_proves_confirmed_challenge_bound_alarm_and_ok_delivery():
    module = _script("exercise_alert_delivery")
    account_id = "123456789012"
    stage = "demo"
    queue_name = f"hindsight-{stage}-alert-receiver"
    queue_url = f"https://sqs.us-east-1.amazonaws.com/{account_id}/{queue_name}"
    queue_arn = f"arn:aws:sqs:us-east-1:{account_id}:{queue_name}"
    operational_topic = f"arn:aws:sns:us-east-1:{account_id}:hindsight-demo-alerts"
    budget_topic = f"arn:aws:sns:us-east-1:{account_id}:hindsight-demo-budget-alerts"
    alarm_name = "hindsight-demo-exact-release-probe"
    messages = []
    deleted = []

    class Paginator:
        def paginate(self, *, TopicArn):
            yield {
                "Subscriptions": [
                    {
                        "Protocol": "sqs",
                        "Endpoint": queue_arn,
                        "SubscriptionArn": f"{TopicArn}:subscription-id",
                    }
                ]
            }

    class SNS:
        def get_paginator(self, operation):
            assert operation == "list_subscriptions_by_topic"
            return Paginator()

        def get_subscription_attributes(self, *, SubscriptionArn):
            assert SubscriptionArn.endswith(":subscription-id")
            return {"Attributes": {"PendingConfirmation": "false"}}

    class SQS:
        def get_queue_url(self, **kwargs):
            assert kwargs == {
                "QueueName": queue_name,
                "QueueOwnerAWSAccountId": account_id,
            }
            return {"QueueUrl": queue_url}

        def get_queue_attributes(self, **kwargs):
            assert kwargs["QueueUrl"] == queue_url
            return {
                "Attributes": {
                    "QueueArn": queue_arn,
                    "SqsManagedSseEnabled": "true",
                }
            }

        def receive_message(self, **kwargs):
            assert kwargs["MessageSystemAttributeNames"] == ["All"]
            assert kwargs["VisibilityTimeout"] == 1
            return {"Messages": [messages.pop(0)]} if messages else {}

        def delete_message(self, **kwargs):
            deleted.append(kwargs["ReceiptHandle"])

    class CloudWatch:
        state = "OK"

        def describe_alarms(self, **kwargs):
            assert kwargs == {"AlarmNames": [alarm_name]}
            return {
                "MetricAlarms": [
                    {
                        "AlarmName": alarm_name,
                        "AlarmArn": f"arn:aws:cloudwatch:us-east-1:{account_id}:alarm:{alarm_name}",
                        "Namespace": "Hindsight/Release",
                        "MetricName": "ExactReleaseProbe",
                        "Dimensions": [
                            {"Name": "Stage", "Value": stage},
                            {"Name": "ReleaseRevision", "Value": SOURCE_REVISION},
                        ],
                        "AlarmActions": [operational_topic],
                        "OKActions": [operational_topic],
                        "StateValue": self.state,
                    }
                ]
            }

        def set_alarm_state(self, **kwargs):
            assert kwargs["AlarmName"] == alarm_name
            assert SOURCE_REVISION in kwargs["StateReasonData"]
            self.state = kwargs["StateValue"]
            notification = {
                "Type": "Notification",
                "TopicArn": operational_topic,
                "MessageId": f"sns-{self.state.lower()}",
                "Message": json.dumps(
                    {
                        "AlarmName": alarm_name,
                        "NewStateValue": self.state,
                        "NewStateReason": kwargs["StateReason"],
                        "StateChangeTime": "2026-08-14T12:00:00.000+0000",
                    }
                ),
            }
            messages.append(
                {
                    "MessageId": f"sqs-{self.state.lower()}",
                    "ReceiptHandle": f"receipt-{self.state.lower()}",
                    "Body": json.dumps(notification),
                }
            )

    sns = SNS()
    sqs = SQS()
    cloudwatch = CloudWatch()

    class Session:
        def client(self, service, *, region_name, config):
            assert region_name == "us-east-1"
            assert config is module.CLIENT_CONFIG
            return {"sns": sns, "sqs": sqs, "cloudwatch": cloudwatch}[service]

    evidence = module.exercise(
        alarm_name=alarm_name,
        receiver_queue_name=queue_name,
        operational_topic_arn=operational_topic,
        budget_topic_arn=budget_topic,
        expected_account_id=account_id,
        region="us-east-1",
        stage=stage,
        profile="test",
        source_revision=SOURCE_REVISION,
        session=Session(),
        challenge="b" * 32,
        timeout_seconds=1,
    )
    assert [transition["state"] for transition in evidence["transitions"]] == [
        "ALARM",
        "OK",
    ]
    assert all(transition["deleted"] for transition in evidence["transitions"])
    assert deleted == ["receipt-alarm", "receipt-ok"]
    assert {row["status"] for row in evidence["receiver"]["subscriptions"]} == {"confirmed"}
    assert evidence["source_revision"] == SOURCE_REVISION
    assert evidence["challenge"] == "b" * 32
    assert evidence["account_id"] == account_id
    assert evidence["region"] == "us-east-1"


def test_alert_exercise_restores_ok_when_alarm_call_fails_after_acceptance(monkeypatch):
    module = _script("exercise_alert_delivery")
    account_id = "123456789012"
    stage = "demo"
    alarm_name = f"hindsight-{stage}-exact-release-probe"
    queue_name = f"hindsight-{stage}-alert-receiver"
    operational_topic = f"arn:aws:sns:us-east-1:{account_id}:hindsight-{stage}-alerts"
    budget_topic = f"arn:aws:sns:us-east-1:{account_id}:hindsight-{stage}-budget-alerts"
    queue_arn = f"arn:aws:sqs:us-east-1:{account_id}:{queue_name}"
    states = []

    class Session:
        def client(self, service, *, region_name, config):
            return object()

    monkeypatch.setattr(
        module,
        "_receiver_queue",
        lambda *args, **kwargs: ("https://sqs.example.invalid/receiver", queue_arn),
    )
    monkeypatch.setattr(
        module,
        "_confirmed_subscription",
        lambda *args, topic_arn, **kwargs: {
            "topic_arn": topic_arn,
            "subscription_arn": f"{topic_arn}:subscription",
            "protocol": "sqs",
            "endpoint": queue_arn,
            "status": "confirmed",
        },
    )
    monkeypatch.setattr(
        module,
        "_release_alarm",
        lambda *args, **kwargs: {
            "alarm_name": alarm_name,
            "alarm_arn": f"arn:aws:cloudwatch:us-east-1:{account_id}:alarm:{alarm_name}",
            "release_revision": SOURCE_REVISION,
            "stage": stage,
            "initial_state": "OK",
        },
    )

    def set_state(*args, state, **kwargs):
        states.append(state)
        if state == "ALARM":
            raise RuntimeError("client timed out after CloudWatch accepted ALARM")

    monkeypatch.setattr(module, "_set_alarm_state", set_state)
    monkeypatch.setattr(
        module,
        "_receive_and_delete",
        lambda *args, **kwargs: pytest.fail("delivery wait must not start after failed ALARM call"),
    )

    with pytest.raises(RuntimeError, match="timed out"):
        module.exercise(
            alarm_name=alarm_name,
            receiver_queue_name=queue_name,
            operational_topic_arn=operational_topic,
            budget_topic_arn=budget_topic,
            expected_account_id=account_id,
            region="us-east-1",
            stage=stage,
            profile="test",
            source_revision=SOURCE_REVISION,
            session=Session(),
            challenge="b" * 32,
            timeout_seconds=1,
        )
    assert states == ["ALARM", "OK"]


def _acceptance_documents():
    started = datetime(2026, 8, 10, 10, 0, tzinfo=UTC)
    run = {
        "id": 123,
        "run_attempt": 2,
        "head_sha": SOURCE_REVISION,
        "head_branch": "main",
        "event": "workflow_dispatch",
        "path": ".github/workflows/live-acceptance.yml",
        "conclusion": "success",
        "created_at": started.isoformat(),
        "run_started_at": (started + timedelta(minutes=10)).isoformat(),
        "updated_at": (started + timedelta(minutes=30)).isoformat(),
        "repository": {"full_name": "owner/hindsight"},
        "actor": {"login": "owner"},
        "triggering_actor": {"login": "owner"},
    }
    provenance = {
        "repository": "owner/hindsight",
        "run_id": "123",
        "run_attempt": "2",
        "artifact_scope": "workflow_run",
        "head_sha": SOURCE_REVISION,
        "acceptance_mode": "full",
        "deployment_environment": "demo",
        "bounded_observability_enabled": True,
    }
    return run, provenance


def test_observability_provenance_requires_successful_bounded_full_acceptance():
    module = _script("collect_observability_evidence")
    run, provenance = _acceptance_documents()
    start, end = module.validate_provenance(
        run,
        provenance,
        repository="owner/hindsight",
        source_revision=SOURCE_REVISION,
        acceptance_run_id="123",
        acceptance_run_attempt="2",
        deployment_environment="demo",
    )
    assert (end - start).total_seconds() == 40 * 60

    provenance["bounded_observability_enabled"] = False
    with pytest.raises(ValueError, match="did not enable bounded observability"):
        module.validate_provenance(
            run,
            provenance,
            repository="owner/hindsight",
            source_revision=SOURCE_REVISION,
            acceptance_run_id="123",
            acceptance_run_attempt="2",
            deployment_environment="demo",
        )


def test_observability_provenance_requires_latest_workflow_run_artifacts():
    module = _script("collect_observability_evidence")
    run, provenance = _acceptance_documents()

    run["run_attempt"] = 3
    with pytest.raises(ValueError, match="latest run attempt"):
        module.validate_provenance(
            run,
            provenance,
            repository="owner/hindsight",
            source_revision=SOURCE_REVISION,
            acceptance_run_id="123",
            acceptance_run_attempt="2",
            deployment_environment="demo",
        )

    run["run_attempt"] = 2
    provenance["artifact_scope"] = "run_attempt"
    with pytest.raises(ValueError, match="provenance artifact scope"):
        module.validate_provenance(
            run,
            provenance,
            repository="owner/hindsight",
            source_revision=SOURCE_REVISION,
            acceptance_run_id="123",
            acceptance_run_attempt="2",
            deployment_environment="demo",
        )


def test_observability_log_query_is_bounded_and_rejects_secret_fields():
    module = _script("collect_observability_evidence")
    groups = module.expected_log_groups("demo")

    class Logs:
        def __init__(self, message):
            self.message = message
            self.started = None

        def start_query(self, **kwargs):
            self.started = kwargs
            return {"queryId": "query-1"}

        def get_query_results(self, **kwargs):
            assert kwargs == {"queryId": "query-1"}
            return {
                "status": "Complete",
                "statistics": {"bytesScanned": 100, "recordsScanned": 4, "recordsMatched": 1},
                "results": [
                    [
                        {"field": "@timestamp", "value": "2026-08-10 10:00:00.000"},
                        {"field": "@log", "value": f"123456789012:{groups[0]}"},
                        {
                            "field": "@message",
                            "value": (
                                "2026-08-10T10:00:00.000Z request-id INFO "
                                + json.dumps(
                                    self.message,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                )
                            ),
                        },
                    ]
                ],
            }

    safe = {
        "event": "api_request",
        "status": "202",
        "tenant_id": "tenant-1",
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
    }
    client = Logs(safe)
    events, statistics = module.collect_logs(
        client,
        log_groups=groups,
        start=datetime.now(UTC) - timedelta(minutes=1),
        end=datetime.now(UTC),
    )
    assert events[0]["event"] == "api_request"
    assert statistics["bytes_scanned"] == 100
    assert client.started["limit"] == module.MAX_LOG_EVENTS
    assert client.started["logGroupNames"] == groups
    assert 'filter @message like /"event":' in client.started["queryString"]
    assert "| sort @timestamp desc " in client.started["queryString"]
    assert f"| limit {module.MAX_LOG_EVENTS}" in client.started["queryString"]

    client = Logs({**safe, "api_key": "never"})
    with pytest.raises(RuntimeError, match="unexpected field"):
        module.collect_logs(
            client,
            log_groups=groups,
            start=datetime.now(UTC) - timedelta(minutes=1),
            end=datetime.now(UTC),
        )

    with pytest.raises(RuntimeError, match="compact structured event"):
        module._extract_structured_event(json.dumps(safe))


def test_observability_browser_evidence_binds_completed_product_run(tmp_path):
    from tests.test_browser_ui import _browser_operation_receipt

    module = _script("collect_observability_evidence")
    operation = tmp_path / "operation.json"
    def run(label: str, *, approved: bool, primary_action: str) -> dict:
        return {
            "run_id": f"run-{label}",
            "decision_id": f"decision-{label}",
            "status": "completed" if approved else "rejected",
            "reflected_memory_id": None,
            "reads": [],
            "action_trace": {
                "selection": {"fingerprint": "a" * 64},
                "recommendation": {
                    "id": f"recommendation:{label}",
                    "operational_action": {
                        "contract": "payments_retry_amplification.v1",
                        "primary_action": primary_action,
                        "fingerprint": f"operational_action:{primary_action}",
                    },
                },
                "approval": {"approved": approved},
                "execution": {
                    "status": (
                        "recommendation_approved" if approved else "not_executed"
                    )
                },
            },
        }

    before = {
        "contract": "payments_retry_amplification.v1",
        "primary_action": "scale_workers",
        "fingerprint": "operational_action:scale_workers",
    }
    after = {
        "contract": "payments_retry_amplification.v1",
        "primary_action": "throttle_retries",
        "fingerprint": "operational_action:throttle_retries",
    }
    receipt = _browser_operation_receipt(
        operation_id="operation-1",
        observed=[],
        persisted=None,
        signature={
            "namespace": "safe-namespace",
            "operation_id": "operation-1",
            "invalidated_memory_ids": ["memory-1"],
            "bad": run("bad", approved=False, primary_action="scale_workers"),
            "corrected": run(
                "1", approved=True, primary_action="throttle_retries"
            ),
            "action_comparison": {
                "status": "changed",
                "contract": "payments_retry_amplification.v1",
                "before": before,
                "after": after,
                "context": {
                    "prompt_equal": True,
                    "normalized_telemetry_equal": True,
                },
                "memory_correction_proven": True,
                "controlled_pair": True,
            },
        },
        capture_errors=[],
    )
    operation.write_text(json.dumps(receipt))
    run_id, digest = module.validate_browser_evidence(operation)
    assert run_id == "run-1"
    assert digest == module.hashlib.sha256(operation.read_bytes()).hexdigest()

    operation.write_text(
        json.dumps(
            {"signature": {"corrected": {"run_id": "run-1", "status": "failed"}}}
        )
    )
    with pytest.raises(ValueError, match="did not complete"):
        module.validate_browser_evidence(operation)


def test_observability_browser_evidence_rejects_unprojected_nested_fields(tmp_path):
    module = _script("collect_observability_evidence")
    operation = tmp_path / "operation.json"
    base = {
        "signature": {
            "corrected": {
                "run_id": "run-1",
                "status": "completed",
            }
        }
    }
    cases = [
        {**base, "raw_model_payload": {"reasoning_steps": []}},
        {
            "signature": {
                "corrected": {
                    "run_id": "run-1",
                    "status": "completed",
                    "reasoning_steps": [{"decision": "unrestricted"}],
                }
            }
        },
        {
            "signature": {
                "corrected": {
                    "run_id": "run-1",
                    "status": "completed",
                    "operational_action": {
                        "contract": "payments_retry_amplification.v1",
                        "primary_action": "throttle_retries",
                        "fingerprint": "operational_action:valid",
                        "parameters": {"credential": "unrestricted"},
                    },
                }
            }
        },
        {
            "signature": {
                "corrected": {"run_id": "run-1", "status": "completed"},
                "action_comparison": {
                    "status": "changed",
                    "contract": "payments_retry_amplification.v1",
                    "before": None,
                    "after": None,
                    "context": {
                        "prompt_equal": {"prompt": "unrestricted"},
                        "normalized_telemetry_equal": True,
                    },
                    "memory_correction_proven": True,
                    "controlled_pair": True,
                },
            }
        },
        {
            **base,
            "persisted": {
                "events": [],
                "effects": [{"metadata": {"request": "unrestricted"}}],
            },
        },
        {
            **base,
            "capture_errors": [
                {
                    "stage": "database",
                    "type": "capture_failed",
                    "detail": "unrestricted exception",
                }
            ],
        },
    ]

    for value in cases:
        operation.write_text(json.dumps(value))
        with pytest.raises(ValueError, match="unexpected fields|invalid"):
            module.validate_browser_evidence(operation)


def test_observability_browser_evidence_rejects_nested_values_in_scalar_projections(
    tmp_path,
):
    module = _script("collect_observability_evidence")
    operation = tmp_path / "operation.json"
    cases = [
        {
            "signature": {
                "corrected": {
                    "run_id": "run-1",
                    "status": "completed",
                    "read_memory_ids": [
                        {"reasoning_steps": [{"decision": "unrestricted"}]}
                    ],
                }
            }
        },
        {
            "signature": {
                "corrected": {"run_id": "run-1", "status": "completed"}
            },
            "persisted": {
                "invalidated_memory_ids": [{"credential": "unrestricted"}],
                "events": [],
                "effects": [],
            },
        },
        {
            "signature": {
                "corrected": {
                    "run_id": {"prompt": "unrestricted"},
                    "status": "completed",
                }
            }
        },
        {
            "signature": {
                "corrected": {
                    "run_id": "run-1",
                    "status": "completed",
                    "decision_id": "Bearer actual-token",
                }
            }
        },
        {
            "signature": {
                "corrected": {
                    "run_id": "run-1",
                    "status": "completed",
                    "decision_id": '{"reasoning_steps":["unrestricted"]}',
                }
            }
        },
    ]

    for value in cases:
        operation.write_text(json.dumps(value))
        with pytest.raises(ValueError, match="must"):
            module.validate_browser_evidence(operation)


def test_observability_correlation_requires_all_product_boundaries():
    module = _script("collect_observability_evidence")
    trace_id = "a" * 32
    common = {
        "tenant_id": "tenant-1",
        "run_id": "run-1",
        "dispatch_id": "dispatch-1",
        "dispatch_attempt_id": "dispatch-attempt-1",
        "trace_id": trace_id,
        "span_id": "b" * 16,
    }
    logs = [
        {"event": "api_request", "status": "202", "tenant_id": "tenant-1", "trace_id": trace_id},
        {"event": "run_dispatch", "status": "sent", "message_id": "message-1", **common},
        {"event": "worker_record", "status": "completed", "message_id": "message-1", **common},
        {
            "event": "realtime_changefeed",
            "status": "delivered",
            "tenant_id": "tenant-1",
            "run_id": "run-1",
            "trace_id": "c" * 32,
        },
    ]
    traces = {
        trace_id: {
            "xray_trace_id": "1-aaaaaaaa-aaaaaaaaaaaaaaaaaaaaaaaa",
            "nodes": [
                {"name": "hindsight.api.request"},
                {"name": "hindsight.worker.message"},
            ],
        }
    }
    result = module.correlate(logs, traces, product_run_id="run-1")
    assert result["run_id"] == "run-1"
    assert result["dispatch"]["message_id"] == result["worker"]["message_id"]
    assert module.candidate_trace_ids(logs, product_run_id="run-1") == [trace_id]

    with pytest.raises(RuntimeError, match="complete correlation candidate"):
        module.candidate_trace_ids(logs, product_run_id="run-other")

    with pytest.raises(RuntimeError, match="no complete"):
        module.correlate(logs[:-1], traces, product_run_id="run-1")


def test_observability_fetches_only_log_derived_trace_ids():
    module = _script("collect_observability_evidence")
    trace_id = "a" * 32

    class Xray:
        def __init__(self):
            self.calls = []

        def batch_get_traces(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "Traces": [
                    {
                        "Id": "1-aaaaaaaa-aaaaaaaaaaaaaaaaaaaaaaaa",
                        "Duration": 1,
                        "Segments": [
                            {"Document": json.dumps({"name": "hindsight.api.request"})}
                        ],
                    }
                ]
            }

    client = Xray()
    traces = module.collect_traces(client, trace_ids=[trace_id])
    assert client.calls == [{"TraceIds": ["1-aaaaaaaa-aaaaaaaaaaaaaaaaaaaaaaaa"]}]
    assert list(traces) == [trace_id]


def test_observability_retries_only_trace_collection(monkeypatch):
    module = _script("collect_observability_evidence")
    calls = {"logs": 0, "traces": 0}

    def logs(*args, **kwargs):
        calls["logs"] += 1
        return ([{"event": "worker_record"}], {"bytes_scanned": 10})

    def candidates(events, *, product_run_id):
        assert product_run_id == "run-1"
        return ["a" * 32]

    def traces(*args, **kwargs):
        calls["traces"] += 1
        if calls["traces"] == 1:
            raise RuntimeError("trace not indexed yet")
        return {"a" * 32: {"nodes": []}}

    def correlation(events, fetched, *, product_run_id):
        return {"run_id": product_run_id, "trace_id": next(iter(fetched))}

    monkeypatch.setattr(module, "collect_logs", logs)
    monkeypatch.setattr(module, "candidate_trace_ids", candidates)
    monkeypatch.setattr(module, "collect_traces", traces)
    monkeypatch.setattr(module, "correlate", correlation)
    result = module.collect_correlation_evidence(
        object(),
        object(),
        log_groups=["group"],
        start=datetime.now(UTC) - timedelta(minutes=1),
        end=datetime.now(UTC),
        product_run_id="run-1",
        sleep=lambda _: None,
    )
    assert calls == {"logs": 1, "traces": 2}
    assert result[0]["run_id"] == "run-1"
    assert result[-1] == 2


def test_observability_report_has_verifiable_payload_digest():
    module = _script("collect_observability_evidence")
    report = module.build_report(
        source_revision=SOURCE_REVISION,
        repository="owner/hindsight",
        acceptance_run_id="123",
        acceptance_run_attempt="2",
        product_run_id="run-1",
        browser_evidence_sha256="c" * 64,
        deployment_environment="demo",
        identity={"account_id": "123456789012", "caller_arn": "arn:aws:iam::123:role/test", "region": "us-east-1"},
        start=datetime.now(UTC) - timedelta(minutes=1),
        end=datetime.now(UTC),
        log_groups=module.expected_log_groups("demo"),
        correlation={"trace_id": "a" * 32},
        log_statistics={"bytes_scanned": 1},
        trace_ids_requested=1,
        traces_returned=1,
        trace_collection_attempt=1,
        alert={"message_id": "message-1"},
    )
    assert module.validate_report_digest(report)
    assert report["method"]["log_query_attempts"] == 1
    assert report["acceptance"]["product_run_id"] == "run-1"
    report["source_revision"] = "b" * 40
    assert not module.validate_report_digest(report)


def test_correlation_fields_drop_arbitrary_values_and_include_trace_ids():
    from opentelemetry.sdk.trace import TracerProvider
    from hindsight.observability import correlation_fields

    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("correlation"):
        fields = correlation_fields({"tenant_id": "tenant", "secret": "never"})
    assert fields["tenant_id"] == "tenant"
    assert len(fields["trace_id"]) == 32
    assert "secret" not in fields


def test_worker_record_keeps_all_correlation_identities(caplog):
    from hindsight.worker import _log_record_result

    message = {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "run_id": "11111111-1111-4111-8111-111111111111",
        "dispatch_id": "22222222-2222-4222-8222-222222222222",
        "dispatch_attempt_id": "33333333-3333-4333-8333-333333333333",
        "attempt_id": "44444444-4444-4444-8444-444444444444",
    }
    context = type("Context", (), {"aws_request_id": "request-1"})()
    with caplog.at_level("INFO", logger="hindsight.worker"):
        _log_record_result(
            status="succeeded",
            message=message,
            message_id="message-1",
            receive_count="1",
            source_arn="arn:aws:sqs:us-east-1:123456789012:runs",
            context=context,
        )
    event = json.loads(caplog.records[-1].message)
    assert {key: event[key] for key in message} == message


def test_lambda_structured_event_loggers_emit_info_without_root_configuration():
    import logging

    from hindsight import api, realtime, run_dispatch, worker

    for module in (api, realtime, run_dispatch, worker):
        assert module.LOGGER.level == logging.INFO
