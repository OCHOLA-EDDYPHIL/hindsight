"""Asynchronous run-worker behavior."""

import json
import logging
from datetime import timedelta
from types import SimpleNamespace

import pytest


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


def test_memory_operation_claim_precedes_runtime_provider_construction(monkeypatch):
    import hindsight.worker as worker

    provider = object()
    captured = {}
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "runtime_database_url", lambda: "postgresql://db")
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(provider_env={"EMBEDDING_PROVIDER": "deterministic"}),
    )
    monkeypatch.setattr(worker, "embedding_provider_from_env", lambda *_args, **_kwargs: provider)

    def execute(**kwargs):
        captured.update(kwargs)
        return {"provider": kwargs["embedding_provider_factory"]()}

    monkeypatch.setattr(worker, "execute_operation", execute)

    result = worker.process_message(
        {"command": "memory_operation", "operation_id": "operation-1"}
    )

    assert result == {"provider": provider}
    assert captured["db_url"] == "postgresql://db"
    assert captured["embedding_provider_factory"] is not None


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
    settings_calls = []
    database_calls = []

    class FakeSsm:
        def get_parameter(self, *, Name, WithDecryption):
            parameter_calls.append((Name, WithDecryption))
            return {"Parameter": {"Value": "postgresql://hosted/database"}}

    environ = {
        runtime.DATABASE_URL_PARAM_ENV: "/hindsight/test/database-url",
        "AWS_LAMBDA_FUNCTION_NAME": "hindsight-worker",
        "LLM_PROVIDER": "deterministic",
        "EMBEDDING_PROVIDER": "deterministic",
    }

    def resolve_settings():
        settings_calls.append(True)
        return runtime.runtime_settings(
            environ=environ,
            ssm_client=FakeSsm(),
            use_cache=False,
        )

    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **_kwargs: None)
    monkeypatch.setattr(worker, "runtime_settings", resolve_settings)
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **kwargs: database_calls.append(("claim", kwargs))
        or SimpleNamespace(outcome="duplicate", run=None, attempt_id=None),
    )
    monkeypatch.setattr(
        worker,
        "get_run",
        lambda **kwargs: (
            database_calls.append(("get", kwargs)) or {"id": kwargs["run_id"], "status": "existing"}
        ),
    )

    result = worker.process_message({"command": command, "run_id": "run-1"})

    assert result == {"id": "run-1", "status": "existing"}
    assert settings_calls == [True]
    assert parameter_calls == [("/hindsight/test/database-url", True)]
    assert database_calls == [
        (
            "claim",
            {
                "run_id": "run-1",
                "command": command,
                "lease_ttl": timedelta(seconds=300),
                "max_attempts": 3,
                "db_url": "postgresql://hosted/database",
            },
        ),
        (
            "get",
            {"run_id": "run-1", "db_url": "postgresql://hosted/database"},
        ),
    ]


def test_worker_records_progress_and_awaiting_approval(monkeypatch):
    import hindsight.worker as worker
    from hindsight.agent import IncidentAgentResult
    from hindsight.reasoning import DeterministicReasoningProvider

    run = {
        "id": "run-1",
        "thread_id": "thread-1",
        "decision_id": "agent:run-1:plan",
        "incident_slug": "checkout-latency",
        "namespace": "demo:payments",
        "service_slug": "payments-api",
        "user_input": "checkout p99 is above SLO",
    }
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **kwargs: None)
    claim_calls = []
    monkeypatch.setattr(
        worker,
        "claim_run_attempt",
        lambda **kwargs: claim_calls.append(kwargs)
        or SimpleNamespace(outcome="claimed", run=run, attempt_id="attempt-1"),
    )
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
            reasoning_max_attempts=1,
        ),
    )
    monkeypatch.setattr(
        worker,
        "reasoning_provider_from_env",
        lambda env: DeterministicReasoningProvider(),
    )
    transitions = []
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

    result = worker.process_message({"command": "start", "run_id": "run-1"})

    assert claim_calls == [
        {
            "run_id": "run-1",
            "command": "start",
            "lease_ttl": timedelta(seconds=300),
            "max_attempts": 3,
            "db_url": "postgresql://db",
        }
    ]
    assert [item["status"] for item in transitions] == ["planning", "awaiting_approval"]
    assert {item["db_url"] for item in transitions} == {"postgresql://db"}
    assert transitions[0]["fields"]["plan"] == "throttle retries"
    assert transitions[1]["metadata"]["action_trace"]["execution"]["status"] == (
        "awaiting_approval"
    )
    assert "score" not in transitions[1]["metadata"]["action_trace"]
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
            provider_env={"LLM_PROVIDER": "deterministic"},
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
        worker.process_message({"command": "start", "run_id": "run-1"})
    except RuntimeError:
        pass

    assert failures[-1]["attempt_id"] == "attempt-1"
    assert failures[-1]["error_type"] == "RuntimeError"


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

    assert result == {"batchItemFailures": []}
    assert [kwargs["dead_letter"] for _, kwargs in calls] == [False, True]


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
                    "body": '{"command":"memory_operation","operation_id":"operation-1"}',
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
        "operation_id": "operation-1",
        "receive_count": "3",
        "source_arn": "arn:aws:sqs:region:account:runs",
        "status": "failed",
    }


def test_exhausted_source_retries_and_dlq_finalizes(monkeypatch):
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

    with pytest.raises(worker.RunAttemptsExhaustedError):
        worker.process_message({"command": "start", "run_id": "run-1"})
    result = worker.process_message(
        {"command": "start", "run_id": "run-1"}, dead_letter=True
    )

    assert result == {"status": "failed"}
    assert finalized[0]["max_attempts"] == 3
