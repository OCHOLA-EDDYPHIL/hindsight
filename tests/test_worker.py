"""Asynchronous run-worker behavior."""

import json
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest


RUN_DELIVERY = {
    "dispatch_id": "11111111-1111-4111-8111-111111111111",
    "dispatch_attempt_id": "22222222-2222-4222-8222-222222222222",
    "dispatch_sequence": 1,
}
RUN_ID = "33333333-3333-4333-8333-333333333333"
OPERATION_ID = "44444444-4444-4444-8444-444444444444"


def _run_message(*, command="start", run_id=RUN_ID, **fields):
    return {
        "command": command,
        "run_id": run_id,
        **RUN_DELIVERY,
        **fields,
    }


@pytest.fixture(autouse=True)
def _stub_runtime_providers(monkeypatch):
    from tests.fakes import DeterministicEmbeddingProvider, DeterministicReasoningProvider

    monkeypatch.setattr(
        "hindsight.worker.reasoning_provider_from_env",
        lambda *_args, **_kwargs: DeterministicReasoningProvider(),
    )
    monkeypatch.setattr(
        "hindsight.worker.embedding_provider_from_env",
        lambda *_args, **_kwargs: DeterministicEmbeddingProvider(),
    )
    monkeypatch.setattr(
        "hindsight.worker.runtime_database_url",
        lambda: "postgresql://db",
    )


def test_scheduled_worker_reaps_expired_memory_operations(monkeypatch):
    import hindsight.worker as worker

    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "runtime_database_url", lambda: "postgresql://db")
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: (_ for _ in ()).throw(AssertionError("provider settings resolved")),
    )
    monkeypatch.setattr(
        worker,
        "reap_exhausted_operations",
        lambda **kwargs: {"failed": 1, "db_url": kwargs["db_url"]},
    )

    assert worker.handler({"command": "reap_memory_operations"}, None) == {
        "failed": 1,
        "db_url": "postgresql://db",
    }


def test_scheduled_worker_dispatches_pending_run_commands(monkeypatch):
    import hindsight.worker as worker

    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "runtime_database_url", lambda: "postgresql://db")
    monkeypatch.setattr(
        worker,
        "dispatch_run_commands",
        lambda **kwargs: {
            "leased": 2,
            "dispatched": 2,
            "failed": 0,
            "lease_lost": 0,
            "db_url": kwargs["db_url"],
            "limit": kwargs["limit"],
        },
    )

    assert worker.handler({"command": "dispatch_run_commands"}, None) == {
        "leased": 2,
        "dispatched": 2,
        "failed": 0,
        "lease_lost": 0,
        "db_url": "postgresql://db",
        "limit": 100,
    }


def test_scheduled_worker_reports_quarantine_metrics_without_provider_settings(monkeypatch):
    import hindsight.worker as worker

    table = object()
    cloudwatch = object()
    calls = []
    monkeypatch.setenv(worker.QUARANTINE_METRIC_STAGE_ENV, "demo")
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "quarantine_table_from_env", lambda: table)
    monkeypatch.setattr(worker.boto3, "client", lambda *args, **kwargs: cloudwatch)
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: pytest.fail("provider settings resolved during quarantine metric report"),
    )
    monkeypatch.setattr(
        worker,
        "report_quarantine_metrics",
        lambda **kwargs: calls.append(kwargs) or {"count": 2, "oldest_age_seconds": 60},
    )

    result = worker.handler({"command": "report_quarantine_metrics"}, None)

    assert result == {"count": 2, "oldest_age_seconds": 60}
    assert calls == [
        {
            "table": table,
            "cloudwatch_client": cloudwatch,
            "stage": "demo",
            "index_name": "quarantine-status-created-at-index",
            "namespace": "Hindsight/Quarantine",
        }
    ]


def test_memory_operation_claim_precedes_runtime_provider_construction(monkeypatch):
    import hindsight.worker as worker

    provider = object()
    captured = {}
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "runtime_database_url", lambda: "postgresql://db")
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(provider_env={}),
    )
    monkeypatch.setattr(worker, "embedding_provider_from_env", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(worker, "uuid4", lambda: OPERATION_ID)

    def execute(**kwargs):
        captured.update(kwargs)
        return {"provider": kwargs["embedding_provider_factory"]()}

    monkeypatch.setattr(worker, "execute_operation", execute)

    result = worker.process_message(
        {
            "command": "memory_operation",
            "operation_id": OPERATION_ID,
            "worker_id": "attacker-controlled worker prose",
        }
    )

    assert result == {"provider": provider}
    assert captured["db_url"] == "postgresql://db"
    assert captured["embedding_provider_factory"] is not None
    assert captured["worker_id"] == f"sqs-worker:{OPERATION_ID}"


def test_worker_trace_attributes_drop_noncanonical_envelope_values(monkeypatch):
    from contextlib import nullcontext

    import hindsight.worker as worker

    captured = []
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "start_span",
        lambda _name, attributes, **_kwargs: captured.append(attributes) or nullcontext(),
    )
    monkeypatch.setattr(worker, "reap_exhausted_operations", lambda **_kwargs: {"failed": 0})

    result = worker.process_message(
        {
            "command": "reap_memory_operations",
            "tenant_id": "00000000-0000-0000-0000-000000000001",
            "run_id": RUN_ID,
            "dispatch_id": "attacker prose and secrets",
            "dispatch_attempt_id": RUN_DELIVERY["dispatch_attempt_id"],
        }
    )

    assert result == {"failed": 0}
    assert captured == [
        {
            "hindsight.tenant_id": "00000000-0000-0000-0000-000000000001",
            "hindsight.run_id": RUN_ID,
            "hindsight.dispatch_attempt_id": RUN_DELIVERY["dispatch_attempt_id"],
        }
    ]


@pytest.mark.parametrize(
    ("command", "expected_status", "next_status"),
    [
        ("start", "queued", "triaging"),
        ("resume", "resuming", "reflecting"),
    ],
)
def test_run_claim_and_duplicate_lookup_share_hosted_database_parameter(
    monkeypatch, command, expected_status, next_status
):
    import hindsight.runtime as runtime
    import hindsight.worker as worker

    parameter_calls = []
    database_calls = []

    class FakeSsm:
        def get_parameter(self, *, Name, WithDecryption):
            parameter_calls.append((Name, WithDecryption))
            return {"Parameter": {"Value": "postgresql://hosted/database"}}

    environ = {
        runtime.DATABASE_URL_PARAM_ENV: "/hindsight/test/database-url",
        "AWS_LAMBDA_FUNCTION_NAME": "hindsight-worker",
        "LLM_PROVIDER": "gemini",
        "EMBEDDING_PROVIDER": "gemini",
    }

    def resolve_database():
        return runtime.runtime_database_url(
            environ=environ,
            ssm_client=FakeSsm(),
        )

    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "runtime_database_url", resolve_database)
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: pytest.fail("provider settings must not resolve for a duplicate delivery"),
    )
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **kwargs: (
            database_calls.append(("claim", kwargs))
            or SimpleNamespace(outcome="duplicate", run=None, attempt_id=None)
        ),
    )
    monkeypatch.setattr(
        worker,
        "get_run",
        lambda **kwargs: (
            database_calls.append(("get", kwargs)) or {"id": kwargs["run_id"], "status": "existing"}
        ),
    )

    result = worker.process_message(_run_message(command=command))

    assert result == {"id": RUN_ID, "status": "existing"}
    assert parameter_calls == [("/hindsight/test/database-url", True)]
    assert database_calls == [
        (
            "claim",
            {
                "run_id": RUN_ID,
                "command": command,
                "command_generation": 0,
                "lease_ttl": timedelta(seconds=300),
                "max_attempts": 3,
                **RUN_DELIVERY,
                "worker_message_id": f"direct:{RUN_DELIVERY['dispatch_attempt_id']}",
                "db_url": "postgresql://hosted/database",
            },
        ),
        (
            "get",
            {"run_id": RUN_ID, "db_url": "postgresql://hosted/database"},
        ),
    ]


@pytest.mark.parametrize(
    ("command", "field", "value", "remove", "error"),
    [
        ("start", "dispatch_id", None, True, "dispatch_id and dispatch_attempt_id are required"),
        (
            "resume",
            "dispatch_attempt_id",
            " ",
            False,
            "dispatch_id and dispatch_attempt_id are required",
        ),
        (
            "start",
            "dispatch_sequence",
            None,
            True,
            "dispatch_sequence must be a positive integer",
        ),
        (
            "resume",
            "dispatch_sequence",
            0,
            False,
            "dispatch_sequence must be a positive integer",
        ),
        (
            "start",
            "dispatch_sequence",
            True,
            False,
            "dispatch_sequence must be a positive integer",
        ),
        (
            "start",
            "dispatch_sequence",
            "1",
            False,
            "dispatch_sequence must be a positive integer",
        ),
    ],
)
def test_run_commands_reject_missing_or_invalid_delivery_identity(
    monkeypatch, command, field, value, remove, error
):
    import hindsight.worker as worker

    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: pytest.fail("delivery identity must be validated before database setup"),
    )
    message = _run_message(command=command)
    if remove:
        message.pop(field)
    else:
        message[field] = value

    with pytest.raises(ValueError, match=error):
        worker.process_message(message)


def test_sqs_message_id_is_passed_to_run_delivery_claim(monkeypatch):
    import hindsight.worker as worker

    claims = []
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(database_url="postgresql://db"),
    )
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **kwargs: (
            claims.append(kwargs) or SimpleNamespace(outcome="duplicate", run=None, attempt_id=None)
        ),
    )
    monkeypatch.setattr(
        worker,
        "get_run",
        lambda **kwargs: {"id": kwargs["run_id"], "status": "existing"},
    )

    result = worker.handler(
        {
            "Records": [
                {
                    "messageId": "sqs-message-1",
                    "eventSourceARN": "arn:aws:sqs:region:account:runs",
                    "body": json.dumps(_run_message()),
                }
            ]
        },
        SimpleNamespace(aws_request_id="request-1"),
    )

    assert result == {"batchItemFailures": []}
    assert claims == [
        {
            "run_id": RUN_ID,
            "command": "start",
            "command_generation": 0,
            "lease_ttl": timedelta(seconds=300),
            "max_attempts": 3,
            **RUN_DELIVERY,
            "worker_message_id": "sqs-message-1",
            "db_url": "postgresql://db",
        }
    ]


def test_run_claim_precedes_provider_settings_resolution(monkeypatch):
    import hindsight.worker as worker

    order = []
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "runtime_database_url",
        lambda: order.append("database") or "postgresql://db",
    )
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **_kwargs: (
            order.append("claim")
            or SimpleNamespace(
                outcome="claimed",
                attempt_id="attempt-1",
                run={
                    "id": "run-1",
                    "thread_id": "thread-1",
                    "decision_id": "agent:run-1:plan",
                    "incident_slug": "incident-1",
                    "namespace": "demo:payments",
                    "service_slug": None,
                    "user_input": "latency",
                },
            )
        ),
    )
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: (
            order.append("provider_settings")
            or (_ for _ in ()).throw(RuntimeError("provider unavailable"))
        ),
    )
    monkeypatch.setattr(worker, "record_run_attempt_failure", lambda **_kwargs: {})

    with pytest.raises(RuntimeError, match="provider unavailable"):
        worker.process_message(_run_message())

    assert order == ["database", "claim", "provider_settings"]


def test_worker_records_progress_and_awaiting_approval(monkeypatch):
    import hindsight.worker as worker
    from hindsight.agent import IncidentAgentResult

    run = {
        "id": "run-1",
        "thread_id": "thread-1",
        "decision_id": "agent:run-1:plan",
        "incident_slug": "checkout-latency",
        "namespace": "demo:payments",
        "service_slug": "payments-api",
        "user_input": "checkout p99 is above SLO",
        "model_call_count": 1,
        "cloudwatch_call_count": 1,
    }
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **kwargs: None)
    monkeypatch.setattr(
        worker,
        "optional_cloudwatch_diagnostics_from_env",
        lambda: SimpleNamespace(name="aws_cloudwatch_diagnostics", query_keys=()),
    )
    claim_calls = []
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **kwargs: (
            claim_calls.append(kwargs)
            or SimpleNamespace(outcome="claimed", run=run, attempt_id="attempt-1")
        ),
    )
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://db",
            provider_env={},
            reasoning_max_attempts=1,
        ),
    )
    transitions = []
    reservations = []
    monkeypatch.setattr(
        worker,
        "reserve_run_budget",
        lambda **kwargs: reservations.append(kwargs) or 2,
    )
    monkeypatch.setattr(
        worker,
        "transition_run_attempt",
        lambda **kwargs: transitions.append(kwargs) or {"id": kwargs["run_id"], **kwargs},
    )
    monkeypatch.setattr(
        worker,
        "finish_run_attempt",
        lambda **kwargs: transitions.append(kwargs) or {"id": kwargs["run_id"], **kwargs},
    )

    def fake_agent(*args, **kwargs):
        assert kwargs["db_url"] == "postgresql://db"
        assert kwargs["initial_model_call_count"] == 1
        assert kwargs["initial_diagnostic_call_count"] == 1
        assert kwargs["model_call_reservation"]() == 2
        assert kwargs["diagnostic_call_reservation"]() == 2
        kwargs["progress_callback"]("diagnostic", "planning", {})
        kwargs["progress_callback"](
            "plan",
            "planning",
            {
                "plan": "throttle retries",
                "reasoning": {
                    "provider": "deterministic",
                    "model": "deterministic-v1",
                    "usage": {},
                },
            },
        )
        return IncidentAgentResult(
            thread_id="thread-1",
            interrupted=True,
            interrupt={
                "proposed_action": "review throttle",
                "action_trace": {
                    "request": {"id": "action:run-1:request"},
                    "execution": {"status": "awaiting_approval"},
                },
            },
            state={},
            plan="throttle retries",
        )

    monkeypatch.setattr(worker, "run_incident_agent", fake_agent)

    result = worker.process_message(_run_message())

    assert claim_calls == [
        {
            "run_id": RUN_ID,
            "command": "start",
            "command_generation": 0,
            "lease_ttl": timedelta(seconds=300),
            "max_attempts": 3,
            **RUN_DELIVERY,
            "worker_message_id": f"direct:{RUN_DELIVERY['dispatch_attempt_id']}",
            "db_url": "postgresql://db",
        }
    ]
    assert [item["status"] for item in transitions] == [
        "planning",
        "planning",
        "awaiting_approval",
    ]
    assert transitions[0]["phase"] == "diagnostic"
    assert transitions[0]["command"] == "start"
    assert {item["db_url"] for item in transitions} == {"postgresql://db"}
    assert transitions[1]["fields"]["plan"] == "throttle retries"
    assert transitions[2]["metadata"]["action_trace"]["execution"]["status"] == (
        "awaiting_approval"
    )
    assert "score" not in transitions[2]["metadata"]["action_trace"]
    assert [item["budget"] for item in reservations] == ["model", "cloudwatch"]
    assert result["status"] == "awaiting_approval"


def test_provider_setup_failure_uses_durable_attempt_retry_path(monkeypatch):
    import hindsight.worker as worker

    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **kwargs: None)
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **kwargs: SimpleNamespace(
            outcome="claimed",
            attempt_id="attempt-1",
            run={
                "id": "run-1",
                "thread_id": "thread-1",
                "decision_id": "agent:run-1:plan",
                "incident_slug": "incident-1",
                "namespace": "demo:payments",
                "service_slug": None,
                "user_input": "latency",
            },
        ),
    )
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://db",
            provider_env={},
            reasoning_max_attempts=1,
        ),
    )
    monkeypatch.setattr(
        worker,
        "reasoning_provider_from_env",
        lambda env: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    failures = []
    monkeypatch.setattr(
        worker,
        "record_run_attempt_failure",
        lambda **kwargs: failures.append(kwargs) or {},
    )

    try:
        worker.process_message(_run_message())
    except RuntimeError:
        pass

    assert failures[-1]["attempt_id"] == "attempt-1"
    assert failures[-1]["error_type"] == "RuntimeError"


@pytest.mark.parametrize("ledger_fails", [False, True])
def test_final_provider_setup_failure_terminalizes_before_source_ack(monkeypatch, ledger_fails):
    import hindsight.worker as worker

    order = []
    run = {
        "id": "run-1",
        "thread_id": "thread-1",
        "decision_id": "agent:run-1:plan",
        "incident_slug": "incident-1",
        "namespace": "demo:payments",
        "service_slug": None,
        "user_input": "latency",
        "worker_attempt_count": 3,
    }
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **_kwargs: (
            order.append("claim")
            or SimpleNamespace(outcome="claimed", attempt_id="attempt-3", run=run)
        ),
    )
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: (
            order.append("provider_settings")
            or (_ for _ in ()).throw(RuntimeError("provider unavailable"))
        ),
    )
    monkeypatch.setattr(
        worker,
        "record_run_attempt_failure",
        lambda **_kwargs: order.append("record_failure") or run,
    )
    finalized = []
    monkeypatch.setattr(
        worker,
        "finalize_exhausted_run",
        lambda **kwargs: (
            order.append("finalize") or finalized.append(kwargs) or {**run, "status": "failed"}
        ),
    )
    monkeypatch.setattr(worker, "quarantine_table_from_env", lambda: object())

    def persist(**kwargs):
        order.append("persist")
        if ledger_fails:
            raise RuntimeError("ledger unavailable")
        return SimpleNamespace(item={"quarantine_id": "q_" + "2" * 64}, created=True)

    monkeypatch.setattr(worker, "persist_quarantine_record", persist)
    result = worker.handler(
        {
            "Records": [
                {
                    "messageId": "message-3",
                    "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:runs",
                    "attributes": {"ApproximateReceiveCount": "3"},
                    "body": json.dumps(_run_message()),
                }
            ]
        },
        SimpleNamespace(aws_request_id="request-3"),
    )

    assert order == ["claim", "provider_settings", "record_failure", "finalize", "persist"]
    assert finalized[0]["attempt_id"] == "attempt-3"
    assert result == {
        "batchItemFailures": ([{"itemIdentifier": "message-3"}] if ledger_fails else [])
    }


def test_resume_attempt_can_replan_and_return_to_approval(monkeypatch):
    import hindsight.worker as worker
    from hindsight.agent import IncidentAgentResult

    run = {
        "id": "run-1",
        "thread_id": "thread-1",
        "decision_id": "agent:run-1:plan",
        "incident_slug": "checkout-latency",
        "namespace": "demo:payments",
        "service_slug": "payments-api",
        "user_input": "checkout p99 is above SLO",
        "model_call_count": 2,
        "cloudwatch_call_count": 1,
    }
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://db",
            provider_env={},
            reasoning_max_attempts=1,
        ),
    )
    monkeypatch.setattr(
        worker,
        "optional_cloudwatch_diagnostics_from_env",
        lambda: SimpleNamespace(name="aws_cloudwatch_diagnostics", query_keys=()),
    )
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **_kwargs: SimpleNamespace(outcome="claimed", run=run, attempt_id="attempt-resume"),
    )
    transitions = []
    monkeypatch.setattr(
        worker,
        "transition_run_attempt",
        lambda **kwargs: transitions.append(kwargs) or kwargs,
    )
    monkeypatch.setattr(
        worker,
        "finish_run_attempt",
        lambda **kwargs: transitions.append(kwargs) or kwargs,
    )

    def fake_resume(**kwargs):
        assert kwargs["model_call_count"] == 2
        assert kwargs["diagnostic_call_count"] == 1
        kwargs["progress_callback"]("plan", "planning", {"action_approved": False})
        return IncidentAgentResult(
            thread_id="thread-1",
            interrupted=True,
            interrupt={
                "proposed_action": "review the refreshed recommendation",
                "action_trace": {
                    "recommendation": {"id": "recommendation:refreshed"},
                    "selection": {"fingerprint": "selection:refreshed"},
                },
            },
            state={},
            plan="refreshed plan",
        )

    monkeypatch.setattr(worker, "resume_incident_agent", fake_resume)

    result = worker.process_message(
        _run_message(
            command="resume",
            approved=True,
            recommendation_id="recommendation:original",
            selection_fingerprint="selection:original",
        )
    )

    assert transitions[0]["status"] == "planning"
    assert transitions[0]["command"] == "resume"
    assert transitions[0]["fields"]["action_approved"] is False
    assert transitions[1]["status"] == "awaiting_approval"
    assert transitions[1]["command"] == "resume"
    assert result["status"] == "awaiting_approval"


def test_handler_identifies_dlq_records_by_source_arn(monkeypatch):
    import hindsight.worker as worker

    calls = []
    monkeypatch.setenv(worker.RUN_DLQ_ARN_ENV, "arn:aws:sqs:region:account:run-dlq")
    monkeypatch.setattr(
        worker,
        "process_message",
        lambda message, **kwargs: calls.append((message, kwargs)),
    )

    result = worker.handler(
        {
            "Records": [
                {
                    "messageId": "source-message",
                    "eventSourceARN": "arn:aws:sqs:region:account:runs",
                    "body": '{"command":"start","run_id":"run-1"}',
                },
                {
                    "messageId": "dlq-message",
                    "eventSourceARN": "arn:aws:sqs:region:account:run-dlq",
                    "body": '{"command":"start","run_id":"run-2"}',
                },
            ]
        },
        None,
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "dlq-message"}]}
    assert calls == [
        (
            {"command": "start", "run_id": "run-1"},
            {"worker_message_id": "source-message"},
        )
    ]


def test_handler_quarantines_malformed_body_without_database_work(monkeypatch):
    import hindsight.worker as worker

    writes = []
    monkeypatch.setattr(worker, "quarantine_table_from_env", lambda: object())
    monkeypatch.setattr(
        worker,
        "persist_quarantine_record",
        lambda **kwargs: (
            writes.append(kwargs)
            or SimpleNamespace(item={"quarantine_id": "q_" + "1" * 64}, created=True)
        ),
    )
    monkeypatch.setattr(
        worker,
        "process_message",
        lambda *_args, **_kwargs: pytest.fail("malformed work reached database processing"),
    )

    result = worker.handler(
        {
            "Records": [
                {
                    "messageId": "message-1",
                    "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:runs",
                    "attributes": {"ApproximateReceiveCount": "2"},
                    "body": '{"command":',
                }
            ]
        },
        SimpleNamespace(aws_request_id="request-1"),
    )

    assert result == {"batchItemFailures": []}
    assert writes[0]["raw_body"] == '{"command":'
    assert writes[0]["reason_code"] == "malformed_json"
    assert writes[0]["work_kind"] == "unknown"
    assert writes[0]["receive_count"] == 2


def test_handler_preserves_raw_message_when_quarantine_persistence_fails(monkeypatch):
    import hindsight.worker as worker

    monkeypatch.setattr(worker, "quarantine_table_from_env", lambda: object())
    monkeypatch.setattr(
        worker,
        "persist_quarantine_record",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("ledger unavailable")),
    )

    result = worker.handler(
        {
            "Records": [
                {
                    "messageId": "message-1",
                    "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:runs",
                    "body": "not-json",
                }
            ]
        },
        SimpleNamespace(aws_request_id="request-1"),
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}


def test_malformed_identity_is_not_logged_or_used_for_database_work(monkeypatch, caplog):
    import hindsight.worker as worker

    untrusted = "noncanonical-id arbitrary operator prose"
    writes = []
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(
        worker,
        "runtime_database_url",
        lambda: pytest.fail("malformed identity reached database resolution"),
    )
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **_kwargs: pytest.fail("malformed identity reached a database claim"),
    )
    monkeypatch.setattr(worker, "quarantine_table_from_env", lambda: object())
    monkeypatch.setattr(
        worker,
        "persist_quarantine_record",
        lambda **kwargs: (
            writes.append(kwargs)
            or SimpleNamespace(item={"quarantine_id": "q_" + "4" * 64}, created=True)
        ),
    )
    caplog.set_level(logging.INFO, logger=worker.__name__)

    result = worker.handler(
        {
            "Records": [
                {
                    "messageId": "message-untrusted",
                    "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:runs",
                    "body": json.dumps(
                        {
                            "command": "start",
                            "run_id": untrusted,
                            **RUN_DELIVERY,
                        }
                    ),
                }
            ]
        },
        SimpleNamespace(aws_request_id="request-untrusted"),
    )

    assert result == {"batchItemFailures": []}
    assert writes[0]["run_id"] is None
    assert writes[0]["work_kind"] == "unknown"
    assert untrusted not in caplog.text


def test_delivery_identity_conflict_is_quarantined_without_provider_resolution(monkeypatch):
    import hindsight.worker as worker

    writes = []
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "runtime_database_url", lambda: "postgresql://db")
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **_kwargs: (_ for _ in ()).throw(
            ValueError("dispatch delivery identity does not match the run")
        ),
    )
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: pytest.fail("provider settings resolved after an invalid delivery"),
    )
    monkeypatch.setattr(worker, "quarantine_table_from_env", lambda: object())
    monkeypatch.setattr(
        worker,
        "persist_quarantine_record",
        lambda **kwargs: (
            writes.append(kwargs)
            or SimpleNamespace(item={"quarantine_id": "q_" + "5" * 64}, created=True)
        ),
    )

    result = worker.handler(
        {
            "Records": [
                {
                    "messageId": "message-conflict",
                    "eventSourceARN": "arn:aws:sqs:us-east-1:123456789012:runs",
                    "body": json.dumps(_run_message()),
                }
            ]
        },
        SimpleNamespace(aws_request_id="request-conflict"),
    )

    assert result == {"batchItemFailures": []}
    assert writes[0]["reason_code"] == "invalid_envelope"
    assert writes[0]["run_id"] is None


def test_handler_logs_safe_sqs_failure_context(monkeypatch, caplog):
    import hindsight.worker as worker

    monkeypatch.setattr(
        worker,
        "process_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("restricted worker cannot inspect referenced row")
        ),
    )
    caplog.set_level(logging.ERROR, logger=worker.__name__)

    result = worker.handler(
        {
            "Records": [
                {
                    "messageId": "message-1",
                    "eventSourceARN": "arn:aws:sqs:region:account:runs",
                    "attributes": {"ApproximateReceiveCount": "3"},
                    "body": json.dumps(
                        {"command": "memory_operation", "operation_id": OPERATION_ID}
                    ),
                }
            ]
        },
        SimpleNamespace(aws_request_id="request-1"),
    )

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}
    record = json.loads(caplog.records[-1].message)
    assert record == {
        "command": "memory_operation",
        "error_code": "PermissionError",
        "error_detail": "restricted worker cannot inspect referenced row",
        "event": "worker_record",
        "lambda_request_id": "request-1",
        "message_id": "message-1",
        "operation_id": OPERATION_ID,
        "receive_count": "3",
        "source_arn": "arn:aws:sqs:region:account:runs",
        "status": "failed",
    }


def test_exhausted_source_finalizes_before_quarantine(monkeypatch):
    import hindsight.worker as worker

    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **kwargs: None)
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(database_url="postgresql://db"),
    )
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **kwargs: SimpleNamespace(outcome="exhausted", run={}, attempt_id=None),
    )
    finalized = []
    monkeypatch.setattr(
        worker,
        "finalize_exhausted_run",
        lambda **kwargs: finalized.append(kwargs) or {"status": "failed"},
    )

    with pytest.raises(worker.TerminalWorkerMessage) as terminal:
        worker.process_message(_run_message())

    assert terminal.value.reason_code == "run_attempts_exhausted"
    assert terminal.value.work_kind == "run"
    assert finalized[0]["max_attempts"] == 3
