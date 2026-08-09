"""Publish explicitly labeled controlled incident telemetry for a safe demonstration."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import math
import pathlib
import re
import sys
import time
from typing import Any

import boto3

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from hindsight.aws import aws_client_config  # noqa: E402
from hindsight.cloudwatch_diagnostics import (  # noqa: E402
    CONTROLLED_SCENARIO,
    CONTROLLED_SERVICE,
    CONTROLLED_TELEMETRY_NAMESPACE,
)

_STAGE_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_METRIC_STATISTICS = {
    "CheckoutLatencyMs": "Average",
    "RetryFanout": "Maximum",
    "ProcessorQueueDepth": "Maximum",
}


def controlled_timestamp(now: datetime) -> datetime:
    """Use a completed prior period so bounded reads can observe the fixture."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("current time must be timezone-aware")
    return now.astimezone(UTC).replace(second=0, microsecond=0) - timedelta(minutes=2)


def controlled_metric_data(
    *,
    stage: str,
    timestamp: datetime,
    checkout_latency_ms: float,
    retry_fanout: float,
    processor_queue_depth: float,
) -> list[dict[str, Any]]:
    """Build the fixed, visibly controlled metric series used by the demo."""

    if not _STAGE_PATTERN.fullmatch(stage):
        raise ValueError("stage must be a lowercase deployment identifier")
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    values = {
        "CheckoutLatencyMs": checkout_latency_ms,
        "RetryFanout": retry_fanout,
        "ProcessorQueueDepth": processor_queue_depth,
    }
    if any(
        isinstance(value, bool) or not math.isfinite(value) or value < 0
        for value in values.values()
    ):
        raise ValueError("controlled telemetry values must be finite and non-negative")
    dimensions = [
        {"Name": "Environment", "Value": stage},
        {"Name": "Scenario", "Value": CONTROLLED_SCENARIO},
        {"Name": "Service", "Value": CONTROLLED_SERVICE},
    ]
    units = {
        "CheckoutLatencyMs": "Milliseconds",
        "RetryFanout": "Count",
        "ProcessorQueueDepth": "Count",
    }
    return [
        {
            "MetricName": name,
            "Dimensions": dimensions,
            "Timestamp": timestamp.astimezone(UTC),
            "Value": float(value),
            "Unit": units[name],
        }
        for name, value in values.items()
    ]


def wait_for_controlled_metric_data(
    *,
    client: Any,
    stage: str,
    timestamp: datetime,
    timeout_seconds: float,
    poll_seconds: float = 5,
) -> None:
    """Wait until all three controlled series are readable from CloudWatch."""

    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("telemetry visibility timeouts must be positive")
    dimensions = [
        {"Name": "Environment", "Value": stage},
        {"Name": "Scenario", "Value": CONTROLLED_SCENARIO},
        {"Name": "Service", "Value": CONTROLLED_SERVICE},
    ]
    deadline = time.monotonic() + timeout_seconds
    pending = set(_METRIC_STATISTICS)
    while pending:
        for metric_name in sorted(pending):
            response = client.get_metric_statistics(
                Namespace=CONTROLLED_TELEMETRY_NAMESPACE,
                MetricName=metric_name,
                Dimensions=dimensions,
                StartTime=timestamp,
                EndTime=timestamp + timedelta(minutes=1),
                Period=60,
                Statistics=[_METRIC_STATISTICS[metric_name]],
            )
            if response.get("Datapoints"):
                pending.remove(metric_name)
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "controlled CloudWatch telemetry did not become visible: "
                + ", ".join(sorted(pending))
            )
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--checkout-latency-ms", type=float, required=True)
    parser.add_argument("--retry-fanout", type=float, required=True)
    parser.add_argument("--processor-queue-depth", type=float, required=True)
    parser.add_argument("--visibility-timeout-seconds", type=float, default=120)
    parser.add_argument(
        "--confirm-controlled-fixture",
        action="store_true",
        help="confirm these values are controlled demonstration telemetry",
    )
    args = parser.parse_args()
    if not args.confirm_controlled_fixture:
        parser.error("--confirm-controlled-fixture is required")

    client = boto3.client(
        "cloudwatch",
        region_name=args.region,
        config=aws_client_config(),
    )
    timestamp = controlled_timestamp(datetime.now(UTC))
    client.put_metric_data(
        Namespace=CONTROLLED_TELEMETRY_NAMESPACE,
        MetricData=controlled_metric_data(
            stage=args.stage,
            timestamp=timestamp,
            checkout_latency_ms=args.checkout_latency_ms,
            retry_fanout=args.retry_fanout,
            processor_queue_depth=args.processor_queue_depth,
        ),
        StrictEntityValidation=True,
    )
    wait_for_controlled_metric_data(
        client=client,
        stage=args.stage,
        timestamp=timestamp,
        timeout_seconds=args.visibility_timeout_seconds,
    )
    print(
        f"published controlled telemetry to {CONTROLLED_TELEMETRY_NAMESPACE} "
        f"for {CONTROLLED_SERVICE}/{CONTROLLED_SCENARIO} at {timestamp.isoformat()}"
    )


if __name__ == "__main__":
    main()
