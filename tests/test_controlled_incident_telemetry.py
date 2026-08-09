"""Tests for the controlled CloudWatch telemetry publisher."""

from datetime import UTC, datetime
import importlib.util
from pathlib import Path

import pytest


def _module():
    path = Path(__file__).parents[1] / "scripts" / "publish_controlled_incident_telemetry.py"
    spec = importlib.util.spec_from_file_location("controlled_incident_telemetry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controlled_metric_payload_is_fixed_and_explicitly_dimensioned():
    module = _module()
    timestamp = module.controlled_timestamp(datetime(2026, 8, 9, 12, 2, 59, tzinfo=UTC))
    payload = module.controlled_metric_data(
        stage="demo",
        timestamp=timestamp,
        checkout_latency_ms=842.5,
        retry_fanout=8,
        processor_queue_depth=217,
    )

    assert [item["MetricName"] for item in payload] == [
        "CheckoutLatencyMs",
        "RetryFanout",
        "ProcessorQueueDepth",
    ]
    assert timestamp == datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    assert all(
        {dimension["Name"] for dimension in item["Dimensions"]}
        == {"Environment", "Scenario", "Service"}
        for item in payload
    )


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_controlled_metric_payload_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="finite and non-negative"):
        _module().controlled_metric_data(
            stage="demo",
            timestamp=datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
            checkout_latency_ms=value,
            retry_fanout=1,
            processor_queue_depth=1,
        )


def test_visibility_wait_reads_each_fixed_metric_series():
    module = _module()

    class VisibleCloudWatch:
        def __init__(self):
            self.calls = []

        def get_metric_statistics(self, **kwargs):
            self.calls.append(kwargs)
            return {"Datapoints": [{"Timestamp": kwargs["StartTime"]}]}

    client = VisibleCloudWatch()
    timestamp = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    module.wait_for_controlled_metric_data(
        client=client,
        stage="demo",
        timestamp=timestamp,
        timeout_seconds=1,
    )

    assert {call["MetricName"] for call in client.calls} == {
        "CheckoutLatencyMs",
        "RetryFanout",
        "ProcessorQueueDepth",
    }
    assert all(call["StartTime"] == timestamp for call in client.calls)
