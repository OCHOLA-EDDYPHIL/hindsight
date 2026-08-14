"""Tests for bounded read-only CloudWatch diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, ReadTimeoutError


class FakeCloudWatch:
    def __init__(self, response=None, *, error: Exception | None = None):
        self.response = response if response is not None else {"Datapoints": []}
        self.error = error
        self.calls = []

    def get_metric_statistics(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _query(**overrides):
    from hindsight.cloudwatch_diagnostics import CloudWatchDimension, CloudWatchMetricQuery

    values = {
        "namespace": "AWS/Lambda",
        "metric_name": "Duration",
        "dimensions": (
            CloudWatchDimension("Stage", "demo"),
            CloudWatchDimension("FunctionName", "hindsight-demo-worker"),
        ),
        "statistic": "Maximum",
        "window_seconds": 900,
        "period_seconds": 60,
    }
    values.update(overrides)
    return CloudWatchMetricQuery(**values)


def _config(queries=None):
    from hindsight.cloudwatch_diagnostics import CloudWatchDiagnosticsConfig

    return CloudWatchDiagnosticsConfig(
        account_id="123456789012",
        region="us-east-1",
        queries=queries or {"worker.duration": _query()},
    )


def test_exact_allowlisted_query_returns_deterministic_bounded_observation():
    from hindsight.cloudwatch_diagnostics import (
        CloudWatchCallBudget,
        CloudWatchDiagnostics,
        cloudwatch_query_fingerprint,
    )

    client = FakeCloudWatch(
        {
            "Datapoints": [
                {
                    "Timestamp": datetime(2026, 8, 9, 10, 6, tzinfo=UTC),
                    "Maximum": 80.0,
                    "Unit": "Milliseconds",
                    "Ignored": "raw response data",
                },
                {
                    "Timestamp": datetime(2026, 8, 9, 9, 53, tzinfo=UTC),
                    "Maximum": 120,
                    "Unit": "Milliseconds",
                },
            ],
            "Label": "untrusted response label",
        }
    )
    config = _config(
        {
            "worker.errors": _query(metric_name="Errors", statistic="Sum"),
            "worker.duration": _query(unit="Milliseconds"),
        }
    )
    diagnostics = CloudWatchDiagnostics(
        config,
        client=client,
        clock=lambda: datetime(
            2026,
            8,
            9,
            12,
            7,
            42,
            tzinfo=timezone_plus_two(),
        ),
    )

    observation = diagnostics.observe("worker.duration", budget=CloudWatchCallBudget())

    assert diagnostics.name == "aws_cloudwatch_diagnostics"
    assert diagnostics.query_keys == ("worker.duration", "worker.errors")
    assert client.calls == [
        {
            "Namespace": "AWS/Lambda",
            "MetricName": "Duration",
            "Dimensions": [
                {"Name": "FunctionName", "Value": "hindsight-demo-worker"},
                {"Name": "Stage", "Value": "demo"},
            ],
            "StartTime": datetime(2026, 8, 9, 9, 52, tzinfo=UTC),
            "EndTime": datetime(2026, 8, 9, 10, 7, tzinfo=UTC),
            "Period": 60,
            "Statistics": ["Maximum"],
            "Unit": "Milliseconds",
        }
    ]
    assert observation == {
        "schema_version": 1,
        "tool": "aws_cloudwatch_diagnostics",
        "query_key": "worker.duration",
        "query_fingerprint": cloudwatch_query_fingerprint(
            config=config,
            query_key="worker.duration",
            query=config.queries["worker.duration"],
        ),
        "status": "available",
        "region": "us-east-1",
        "metric": {
            "namespace": "AWS/Lambda",
            "name": "Duration",
            "dimensions": [
                {"name": "FunctionName", "value": "hindsight-demo-worker"},
                {"name": "Stage", "value": "demo"},
            ],
            "statistic": "Maximum",
            "unit": "Milliseconds",
            "period_seconds": 60,
        },
        "window": {
            "start": "2026-08-09T09:52:00Z",
            "end": "2026-08-09T10:07:00Z",
            "seconds": 900,
        },
        "datapoints": [
            {"timestamp": "2026-08-09T09:53:00Z", "value": 120.0},
            {"timestamp": "2026-08-09T10:06:00Z", "value": 80.0},
        ],
        "datapoint_count": 2,
        "truncated": False,
    }


def test_replay_anchor_pins_repeated_reads_to_the_same_metric_window():
    from hindsight.cloudwatch_diagnostics import CloudWatchCallBudget, CloudWatchDiagnostics

    client = FakeCloudWatch(
        {
            "Datapoints": [
                {
                    "Timestamp": datetime(2026, 8, 9, 10, 6, tzinfo=UTC),
                    "Maximum": 8.0,
                }
            ]
        }
    )
    diagnostics = CloudWatchDiagnostics(
        _config(),
        client=client,
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )
    anchor = datetime(2026, 8, 9, 10, 7, 42, tzinfo=UTC)

    first = diagnostics.observe_at_replay_anchor(
        "worker.duration",
        budget=CloudWatchCallBudget(),
        replay_anchor=anchor,
    )
    second = diagnostics.observe_at_replay_anchor(
        "worker.duration",
        budget=CloudWatchCallBudget(),
        replay_anchor=anchor,
    )

    assert first == second
    assert client.calls[0]["StartTime"] == client.calls[1]["StartTime"]
    assert client.calls[0]["EndTime"] == client.calls[1]["EndTime"]


def test_query_allowlist_is_immutable_and_unknown_keys_do_not_consume_budget():
    from hindsight.cloudwatch_diagnostics import (
        CloudWatchCallBudget,
        CloudWatchDiagnostics,
        CloudWatchQueryNotAllowedError,
    )

    configured = {"worker.duration": _query()}
    config = _config(configured)
    configured["caller.injected"] = _query(namespace="Custom/Injected")
    client = FakeCloudWatch()
    diagnostics = CloudWatchDiagnostics(config, client=client)
    budget = CloudWatchCallBudget()

    with pytest.raises(CloudWatchQueryNotAllowedError) as raised:
        diagnostics.observe("caller.injected", budget=budget)

    assert raised.value.error_code == "cloudwatch_query_not_allowed"
    assert diagnostics.query_keys == ("worker.duration",)
    assert budget.used_calls == 0
    assert client.calls == []
    with pytest.raises(TypeError):
        config.queries["other"] = _query()


def test_call_budget_is_shared_and_hard_capped_at_three_calls():
    from hindsight.cloudwatch_diagnostics import (
        CloudWatchCallBudget,
        CloudWatchCallBudgetExceededError,
        CloudWatchDiagnostics,
    )

    client = FakeCloudWatch()
    diagnostics = CloudWatchDiagnostics(_config(), client=client)
    budget = CloudWatchCallBudget()

    for _ in range(3):
        diagnostics.observe("worker.duration", budget=budget)

    with pytest.raises(CloudWatchCallBudgetExceededError) as raised:
        diagnostics.observe("worker.duration", budget=budget)

    assert raised.value.error_code == "cloudwatch_call_budget_exhausted"
    assert budget.used_calls == 3
    assert budget.remaining_calls == 0
    assert len(client.calls) == 3
    with pytest.raises(ValueError, match="between 1 and 3"):
        CloudWatchCallBudget(limit=4)


def test_call_budget_restores_persisted_run_usage():
    from hindsight.cloudwatch_diagnostics import (
        CloudWatchCallBudget,
        CloudWatchCallBudgetExceededError,
        CloudWatchDiagnostics,
    )

    client = FakeCloudWatch()
    budget = CloudWatchCallBudget(initial_used_calls=2)

    CloudWatchDiagnostics(_config(), client=client).observe("worker.duration", budget=budget)

    assert budget.used_calls == 3
    with pytest.raises(CloudWatchCallBudgetExceededError):
        CloudWatchDiagnostics(_config(), client=client).observe("worker.duration", budget=budget)


def test_runtime_factory_exposes_only_controlled_incident_query_keys(monkeypatch):
    from hindsight.cloudwatch_diagnostics import cloudwatch_diagnostics_from_env

    class FakeBoto3:
        @staticmethod
        def client(*args, **kwargs):
            assert args == ("cloudwatch",)
            assert kwargs["region_name"] == "us-east-1"
            return FakeCloudWatch()

    monkeypatch.setattr("hindsight.cloudwatch_diagnostics.boto3", FakeBoto3())

    diagnostics = cloudwatch_diagnostics_from_env(
        {
            "HINDSIGHT_AWS_ACCOUNT_ID": "123456789012",
            "AWS_REGION": "us-east-1",
            "HINDSIGHT_STAGE": "demo",
        }
    )

    assert diagnostics.query_keys == (
        "payments.checkout_latency_ms",
        "payments.processor_queue_depth",
        "payments.retry_fanout",
    )


def test_optional_runtime_factory_disables_only_fully_unconfigured_local_scope(monkeypatch):
    from hindsight.cloudwatch_diagnostics import optional_cloudwatch_diagnostics_from_env

    assert optional_cloudwatch_diagnostics_from_env({"AWS_REGION": "us-east-1"}) is None
    with pytest.raises(ValueError, match="AWS_REGION"):
        optional_cloudwatch_diagnostics_from_env(
            {
                "HINDSIGHT_AWS_ACCOUNT_ID": "123456789012",
                "HINDSIGHT_STAGE": "demo",
            }
        )

    class FakeBoto3:
        @staticmethod
        def client(*_args, **_kwargs):
            return FakeCloudWatch()

    monkeypatch.setattr("hindsight.cloudwatch_diagnostics.boto3", FakeBoto3())
    assert (
        optional_cloudwatch_diagnostics_from_env(
            {
                "HINDSIGHT_AWS_ACCOUNT_ID": "123456789012",
                "AWS_REGION": "us-east-1",
                "HINDSIGHT_STAGE": "demo",
            }
        )
        is not None
    )


def test_client_error_fails_closed_without_exposing_provider_message():
    from hindsight.cloudwatch_diagnostics import (
        CloudWatchCallBudget,
        CloudWatchDiagnostics,
        CloudWatchDiagnosticsUnavailableError,
    )

    client = FakeCloudWatch(
        error=ClientError(
            {
                "Error": {
                    "Code": "AccessDeniedException",
                    "Message": "token=supersecret password=hidden",
                },
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "GetMetricStatistics",
        )
    )
    budget = CloudWatchCallBudget()

    with pytest.raises(CloudWatchDiagnosticsUnavailableError) as raised:
        CloudWatchDiagnostics(_config(), client=client).observe(
            "worker.duration",
            budget=budget,
        )

    assert raised.value.error_code == "cloudwatch_client_error"
    assert "AccessDeniedException" in str(raised.value)
    assert "supersecret" not in str(raised.value)
    assert "hidden" not in str(raised.value)
    assert raised.value.__suppress_context__ is True
    assert budget.used_calls == 1


@pytest.mark.parametrize(
    "error",
    [
        ReadTimeoutError(endpoint_url="https://token=supersecret.example"),
        TimeoutError("password=hidden"),
    ],
)
def test_timeouts_fail_closed_without_exposing_endpoints(error):
    from hindsight.cloudwatch_diagnostics import (
        CloudWatchCallBudget,
        CloudWatchDiagnostics,
        CloudWatchDiagnosticsUnavailableError,
    )

    with pytest.raises(CloudWatchDiagnosticsUnavailableError) as raised:
        CloudWatchDiagnostics(_config(), client=FakeCloudWatch(error=error)).observe(
            "worker.duration",
            budget=CloudWatchCallBudget(),
        )

    assert raised.value.error_code == "cloudwatch_timeout"
    assert "supersecret" not in str(raised.value)
    assert "hidden" not in str(raised.value)


def test_other_sdk_transport_errors_fail_closed():
    from hindsight.cloudwatch_diagnostics import (
        CloudWatchCallBudget,
        CloudWatchDiagnostics,
        CloudWatchDiagnosticsUnavailableError,
    )

    with pytest.raises(CloudWatchDiagnosticsUnavailableError) as raised:
        CloudWatchDiagnostics(
            _config(),
            client=FakeCloudWatch(error=NoCredentialsError()),
        ).observe("worker.duration", budget=CloudWatchCallBudget())

    assert raised.value.error_code == "cloudwatch_transport_error"
    assert str(raised.value) == (
        "CloudWatch diagnostics request failed before a response was available"
    )


def test_observation_is_capped_and_keeps_the_latest_configured_periods():
    from hindsight.cloudwatch_diagnostics import CloudWatchCallBudget, CloudWatchDiagnostics

    datapoints = [
        {
            "Timestamp": datetime(2026, 8, 9, 9, 52, tzinfo=UTC) + timedelta(seconds=index * 30),
            "Maximum": index,
        }
        for index in range(30)
    ]
    client = FakeCloudWatch({"Datapoints": list(reversed(datapoints))})
    diagnostics = CloudWatchDiagnostics(
        _config(),
        client=client,
        clock=lambda: datetime(2026, 8, 9, 10, 7, tzinfo=UTC),
    )

    observation = diagnostics.observe("worker.duration", budget=CloudWatchCallBudget())

    assert observation["datapoint_count"] == 15
    assert observation["truncated"] is True
    assert observation["datapoints"][0] == {
        "timestamp": "2026-08-09T09:59:30Z",
        "value": 15.0,
    }
    assert observation["datapoints"][-1] == {
        "timestamp": "2026-08-09T10:06:30Z",
        "value": 29.0,
    }


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        ({"Datapoints": "not-a-list"}, "cloudwatch_invalid_response"),
        (
            {
                "Datapoints": [
                    {
                        "Timestamp": datetime(2026, 8, 9, 10, 6, tzinfo=UTC),
                        "Maximum": float("nan"),
                    }
                ]
            },
            "cloudwatch_invalid_response",
        ),
        (
            {
                "Datapoints": [
                    {
                        "Timestamp": datetime(2026, 8, 9, 9, 30, tzinfo=UTC),
                        "Maximum": 1,
                    }
                ]
            },
            "cloudwatch_invalid_response",
        ),
    ],
)
def test_malformed_or_out_of_window_response_fails_closed(response, expected_code):
    from hindsight.cloudwatch_diagnostics import (
        CloudWatchCallBudget,
        CloudWatchDiagnostics,
        CloudWatchDiagnosticsResponseError,
    )

    diagnostics = CloudWatchDiagnostics(
        _config(),
        client=FakeCloudWatch(response),
        clock=lambda: datetime(2026, 8, 9, 10, 7, tzinfo=UTC),
    )

    with pytest.raises(CloudWatchDiagnosticsResponseError) as raised:
        diagnostics.observe("worker.duration", budget=CloudWatchCallBudget())

    assert raised.value.error_code == expected_code
    assert str(raised.value) == "CloudWatch diagnostics returned an invalid response"


@pytest.mark.parametrize(
    "overrides",
    [
        {"statistic": "p99"},
        {"window_seconds": 901},
        {"period_seconds": 30},
        {"period_seconds": 61},
        {"window_seconds": 600, "period_seconds": 240},
    ],
)
def test_query_rejects_unsupported_statistic_window_or_period(overrides):
    with pytest.raises(ValueError):
        _query(**overrides)


def test_config_rejects_invalid_account_region_and_duplicate_dimensions():
    from hindsight.cloudwatch_diagnostics import (
        CloudWatchDiagnosticsConfig,
        CloudWatchDimension,
    )

    with pytest.raises(ValueError, match="12-digit"):
        CloudWatchDiagnosticsConfig(
            account_id="caller-account",
            region="us-east-1",
            queries={"worker.duration": _query()},
        )
    with pytest.raises(ValueError, match="region"):
        CloudWatchDiagnosticsConfig(
            account_id="123456789012",
            region="caller-region",
            queries={"worker.duration": _query()},
        )
    with pytest.raises(ValueError, match="unique"):
        _query(
            dimensions=(
                CloudWatchDimension("FunctionName", "one"),
                CloudWatchDimension("FunctionName", "two"),
            )
        )


def test_default_client_is_region_bound_with_one_transport_attempt(monkeypatch):
    import hindsight.cloudwatch_diagnostics as diagnostics_module

    calls = []
    client = FakeCloudWatch()

    def fake_client(service_name, **kwargs):
        calls.append((service_name, kwargs))
        return client

    monkeypatch.setattr(diagnostics_module.boto3, "client", fake_client)

    diagnostics_module.CloudWatchDiagnostics(_config())

    assert calls[0][0] == "cloudwatch"
    assert calls[0][1]["region_name"] == "us-east-1"
    config = calls[0][1]["config"]
    assert config.connect_timeout == 3
    assert config.read_timeout == 5
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}


def timezone_plus_two():
    return timezone(timedelta(hours=2))
