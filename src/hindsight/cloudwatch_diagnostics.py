"""Bounded read-only CloudWatch metric diagnostics."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import Lock
from types import MappingProxyType
from typing import Any, ClassVar, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

TOOL_NAME = "aws_cloudwatch_diagnostics"
CONTROLLED_TELEMETRY_NAMESPACE = "Hindsight/ControlledIncidentTelemetry"
CONTROLLED_SCENARIO = "payments-checkout-latency"
CONTROLLED_SERVICE = "payments-api"
MAX_CALLS_PER_RUN = 3
MAX_WINDOW_SECONDS = 15 * 60
MIN_PERIOD_SECONDS = 60
MAX_PERIOD_SECONDS = 5 * 60
MAX_OBSERVATION_DATAPOINTS = MAX_WINDOW_SECONDS // MIN_PERIOD_SECONDS
MAX_CONFIGURED_QUERIES = 32
MAX_CONFIGURED_DIMENSIONS = 10

_STATISTICS = frozenset({"Average", "Maximum", "Minimum", "SampleCount", "Sum"})
_QUERY_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_ACCOUNT_ID_PATTERN = re.compile(r"[0-9]{12}")
_REGION_PATTERN = re.compile(r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]")
_ERROR_CODE_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,64}")

_CLIENT_CONFIG = Config(
    connect_timeout=3,
    read_timeout=5,
    retries={"total_max_attempts": 1, "mode": "standard"},
)


class CloudWatchClient(Protocol):
    """The only CloudWatch operation available to the diagnostics component."""

    def get_metric_statistics(self, **kwargs: Any) -> Mapping[str, Any]: ...


class CloudWatchDiagnosticsError(RuntimeError):
    """A fail-closed diagnostics error safe to expose to the agent runtime."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


class CloudWatchQueryNotAllowedError(CloudWatchDiagnosticsError):
    """Raised when a caller requests a query outside the server allowlist."""


class CloudWatchCallBudgetExceededError(CloudWatchDiagnosticsError):
    """Raised before an AWS call would exceed the run-scoped budget."""


class CloudWatchDiagnosticsUnavailableError(CloudWatchDiagnosticsError):
    """Raised when CloudWatch cannot return a trusted observation."""


class CloudWatchDiagnosticsResponseError(CloudWatchDiagnosticsError):
    """Raised when an AWS response cannot be safely normalized."""


@dataclass(frozen=True, slots=True)
class CloudWatchDimension:
    """One exact server-configured CloudWatch metric dimension."""

    name: str
    value: str

    def __post_init__(self) -> None:
        _validate_text(self.name, field_name="dimension name", max_chars=255)
        _validate_text(self.value, field_name="dimension value", max_chars=1024)


@dataclass(frozen=True, slots=True)
class CloudWatchMetricQuery:
    """One immutable query selected indirectly by its configured key."""

    namespace: str
    metric_name: str
    dimensions: tuple[CloudWatchDimension, ...] = ()
    statistic: str = "Average"
    window_seconds: int = MAX_WINDOW_SECONDS
    period_seconds: int = MIN_PERIOD_SECONDS

    def __post_init__(self) -> None:
        _validate_text(self.namespace, field_name="namespace", max_chars=255)
        _validate_text(self.metric_name, field_name="metric name", max_chars=255)
        if self.statistic not in _STATISTICS:
            raise ValueError("statistic is not supported")
        if type(self.window_seconds) is not int or not (
            MIN_PERIOD_SECONDS <= self.window_seconds <= MAX_WINDOW_SECONDS
        ):
            raise ValueError("window_seconds must be between 60 and 900")
        if type(self.period_seconds) is not int or not (
            MIN_PERIOD_SECONDS <= self.period_seconds <= MAX_PERIOD_SECONDS
        ):
            raise ValueError("period_seconds must be between 60 and 300")
        if self.period_seconds % MIN_PERIOD_SECONDS != 0:
            raise ValueError("period_seconds must be a multiple of 60")
        if self.period_seconds > self.window_seconds:
            raise ValueError("period_seconds cannot exceed window_seconds")
        if self.window_seconds % self.period_seconds != 0:
            raise ValueError("window_seconds must be divisible by period_seconds")

        dimensions = tuple(self.dimensions)
        if len(dimensions) > MAX_CONFIGURED_DIMENSIONS:
            raise ValueError("too many CloudWatch dimensions")
        if any(not isinstance(item, CloudWatchDimension) for item in dimensions):
            raise TypeError("dimensions must contain CloudWatchDimension values")
        names = [item.name for item in dimensions]
        if len(names) != len(set(names)):
            raise ValueError("CloudWatch dimension names must be unique")
        object.__setattr__(
            self,
            "dimensions",
            tuple(sorted(dimensions, key=lambda item: (item.name, item.value))),
        )


@dataclass(frozen=True, slots=True)
class CloudWatchDiagnosticsConfig:
    """Server-owned account, region, and keyed query allowlist."""

    account_id: str
    region: str
    queries: Mapping[str, CloudWatchMetricQuery]

    def __post_init__(self) -> None:
        if not isinstance(self.account_id, str) or not _ACCOUNT_ID_PATTERN.fullmatch(
            self.account_id
        ):
            raise ValueError("account_id must be a 12-digit AWS account ID")
        if not isinstance(self.region, str) or not _REGION_PATTERN.fullmatch(self.region):
            raise ValueError("region is not a valid AWS region name")
        if not isinstance(self.queries, Mapping) or not self.queries:
            raise ValueError("at least one CloudWatch query is required")
        if len(self.queries) > MAX_CONFIGURED_QUERIES:
            raise ValueError("too many CloudWatch queries are configured")

        queries: dict[str, CloudWatchMetricQuery] = {}
        for key, query in self.queries.items():
            if not isinstance(key, str) or not _QUERY_KEY_PATTERN.fullmatch(key):
                raise ValueError("CloudWatch query keys must be stable lowercase identifiers")
            if not isinstance(query, CloudWatchMetricQuery):
                raise TypeError("queries must contain CloudWatchMetricQuery values")
            queries[key] = query
        object.__setattr__(self, "queries", MappingProxyType(dict(sorted(queries.items()))))


@dataclass(slots=True)
class CloudWatchCallBudget:
    """Thread-safe run-scoped budget consumed before each CloudWatch call."""

    limit: int = MAX_CALLS_PER_RUN
    initial_used_calls: int = field(default=0, repr=False)
    _used_calls: int = field(default=0, init=False, repr=False)
    _lock: Any = field(default_factory=Lock, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.limit) is not int or not 1 <= self.limit <= MAX_CALLS_PER_RUN:
            raise ValueError("CloudWatch call budget limit must be between 1 and 3")
        if type(self.initial_used_calls) is not int or not (
            0 <= self.initial_used_calls <= self.limit
        ):
            raise ValueError("initial CloudWatch calls must be between zero and the limit")
        self._used_calls = self.initial_used_calls

    @property
    def used_calls(self) -> int:
        with self._lock:
            return self._used_calls

    @property
    def remaining_calls(self) -> int:
        with self._lock:
            return self.limit - self._used_calls

    def consume(self) -> None:
        with self._lock:
            if self._used_calls >= self.limit:
                raise CloudWatchCallBudgetExceededError(
                    "cloudwatch_call_budget_exhausted",
                    "CloudWatch diagnostics call budget is exhausted",
                )
            self._used_calls += 1


class CloudWatchDiagnostics:
    """Execute exact allow-listed metric queries through a read-only CloudWatch client."""

    name: ClassVar[str] = TOOL_NAME

    def __init__(
        self,
        config: CloudWatchDiagnosticsConfig,
        *,
        client: CloudWatchClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._config = config
        self._client = (
            client
            if client is not None
            else boto3.client(
                "cloudwatch",
                region_name=config.region,
                config=_CLIENT_CONFIG,
            )
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def query_keys(self) -> tuple[str, ...]:
        """Return the stable query keys accepted from an agent decision."""

        return tuple(self._config.queries)

    def observe(
        self,
        query_key: str,
        *,
        budget: CloudWatchCallBudget,
    ) -> dict[str, Any]:
        """Return one bounded observation for an exact configured query key."""

        query = self._config.queries.get(query_key)
        if query is None:
            raise CloudWatchQueryNotAllowedError(
                "cloudwatch_query_not_allowed",
                "CloudWatch diagnostics query is not allow-listed",
            )

        end_time = _aligned_utc_time(self._clock(), period_seconds=query.period_seconds)
        start_time = end_time - timedelta(seconds=query.window_seconds)
        request = {
            "Namespace": query.namespace,
            "MetricName": query.metric_name,
            "Dimensions": [
                {"Name": dimension.name, "Value": dimension.value} for dimension in query.dimensions
            ],
            "StartTime": start_time,
            "EndTime": end_time,
            "Period": query.period_seconds,
            "Statistics": [query.statistic],
        }
        budget.consume()
        try:
            response = self._client.get_metric_statistics(**request)
        except ClientError as exc:
            raise CloudWatchDiagnosticsUnavailableError(
                "cloudwatch_client_error",
                _redacted_client_error(exc),
            ) from None
        except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError, TimeoutError):
            raise CloudWatchDiagnosticsUnavailableError(
                "cloudwatch_timeout",
                "CloudWatch diagnostics request timed out or could not connect",
            ) from None
        except BotoCoreError:
            raise CloudWatchDiagnosticsUnavailableError(
                "cloudwatch_transport_error",
                "CloudWatch diagnostics request failed before a response was available",
            ) from None

        return _normalize_observation(
            config=self._config,
            query_key=query_key,
            query=query,
            response=response,
            start_time=start_time,
            end_time=end_time,
        )


def cloudwatch_diagnostics_from_env(
    environ: Mapping[str, str] | None = None,
) -> CloudWatchDiagnostics:
    """Build the fixed controlled-incident query set from server identity settings."""

    env = os.environ if environ is None else environ
    account_id = str(env.get("HINDSIGHT_AWS_ACCOUNT_ID") or "").strip()
    region = str(env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip()
    stage = str(env.get("HINDSIGHT_STAGE") or "").strip()
    if not stage:
        raise ValueError("HINDSIGHT_STAGE is required for CloudWatch diagnostics")
    dimensions = (
        CloudWatchDimension("Environment", stage),
        CloudWatchDimension("Scenario", CONTROLLED_SCENARIO),
        CloudWatchDimension("Service", CONTROLLED_SERVICE),
    )
    queries = {
        "payments.checkout_latency_ms": CloudWatchMetricQuery(
            namespace=CONTROLLED_TELEMETRY_NAMESPACE,
            metric_name="CheckoutLatencyMs",
            dimensions=dimensions,
            statistic="Average",
        ),
        "payments.processor_queue_depth": CloudWatchMetricQuery(
            namespace=CONTROLLED_TELEMETRY_NAMESPACE,
            metric_name="ProcessorQueueDepth",
            dimensions=dimensions,
            statistic="Maximum",
        ),
        "payments.retry_fanout": CloudWatchMetricQuery(
            namespace=CONTROLLED_TELEMETRY_NAMESPACE,
            metric_name="RetryFanout",
            dimensions=dimensions,
            statistic="Maximum",
        ),
    }
    return CloudWatchDiagnostics(
        CloudWatchDiagnosticsConfig(
            account_id=account_id,
            region=region,
            queries=queries,
        )
    )


def optional_cloudwatch_diagnostics_from_env(
    environ: Mapping[str, str] | None = None,
) -> CloudWatchDiagnostics | None:
    """Return diagnostics when the Hindsight-specific AWS scope is configured.

    Local product runs intentionally omit the account and stage. A partial
    Hindsight configuration remains an error so hosted deployments cannot
    silently lose their diagnostic boundary.
    """

    env = os.environ if environ is None else environ
    account_id = str(env.get("HINDSIGHT_AWS_ACCOUNT_ID") or "").strip()
    stage = str(env.get("HINDSIGHT_STAGE") or "").strip()
    if not account_id and not stage:
        return None
    region = str(env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "").strip()
    missing = [
        name
        for name, value in (
            ("HINDSIGHT_AWS_ACCOUNT_ID", account_id),
            ("AWS_REGION", region),
            ("HINDSIGHT_STAGE", stage),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "CloudWatch diagnostics configuration is incomplete: " + ", ".join(missing)
        )
    return cloudwatch_diagnostics_from_env(env)


def _normalize_observation(
    *,
    config: CloudWatchDiagnosticsConfig,
    query_key: str,
    query: CloudWatchMetricQuery,
    response: Mapping[str, Any],
    start_time: datetime,
    end_time: datetime,
) -> dict[str, Any]:
    if not isinstance(response, Mapping):
        raise _response_error()
    raw_datapoints = response.get("Datapoints", [])
    if not isinstance(raw_datapoints, list):
        raise _response_error()

    datapoints: list[tuple[datetime, float]] = []
    for raw in raw_datapoints:
        if not isinstance(raw, Mapping):
            raise _response_error()
        timestamp = raw.get("Timestamp")
        value = raw.get(query.statistic)
        if (
            not isinstance(timestamp, datetime)
            or timestamp.tzinfo is None
            or timestamp.utcoffset() is None
            or isinstance(value, bool)
            or not isinstance(value, (int, float, Decimal))
        ):
            raise _response_error()
        normalized_time = timestamp.astimezone(UTC).replace(microsecond=0)
        if not start_time <= normalized_time < end_time:
            raise _response_error()
        normalized_value = float(value)
        if not math.isfinite(normalized_value):
            raise _response_error()
        datapoints.append((normalized_time, 0.0 if normalized_value == 0.0 else normalized_value))

    datapoints.sort(key=lambda item: (item[0], item[1]))
    point_limit = min(
        MAX_OBSERVATION_DATAPOINTS,
        query.window_seconds // query.period_seconds,
    )
    truncated = len(datapoints) > point_limit
    if truncated:
        datapoints = datapoints[-point_limit:]

    return {
        "schema_version": 1,
        "tool": TOOL_NAME,
        "query_key": query_key,
        "status": "available",
        "account_id": config.account_id,
        "region": config.region,
        "metric": {
            "namespace": query.namespace,
            "name": query.metric_name,
            "dimensions": [
                {"name": dimension.name, "value": dimension.value} for dimension in query.dimensions
            ],
            "statistic": query.statistic,
            "period_seconds": query.period_seconds,
        },
        "window": {
            "start": _format_utc(start_time),
            "end": _format_utc(end_time),
            "seconds": query.window_seconds,
        },
        "datapoints": [
            {"timestamp": _format_utc(timestamp), "value": value} for timestamp, value in datapoints
        ],
        "datapoint_count": len(datapoints),
        "truncated": truncated,
    }


def _aligned_utc_time(value: datetime, *, period_seconds: int) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("CloudWatch diagnostics clock must return a timezone-aware datetime")
    epoch_seconds = int(value.astimezone(UTC).timestamp())
    aligned_seconds = epoch_seconds - (epoch_seconds % period_seconds)
    return datetime.fromtimestamp(aligned_seconds, tz=UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redacted_client_error(exc: ClientError) -> str:
    error = exc.response.get("Error", {}) if isinstance(exc.response, Mapping) else {}
    raw_code = error.get("Code") if isinstance(error, Mapping) else None
    code = str(raw_code or "ClientError")
    if not _ERROR_CODE_PATTERN.fullmatch(code):
        code = "ClientError"
    return f"CloudWatch diagnostics request failed ({code})"


def _response_error() -> CloudWatchDiagnosticsResponseError:
    return CloudWatchDiagnosticsResponseError(
        "cloudwatch_invalid_response",
        "CloudWatch diagnostics returned an invalid response",
    )


def _validate_text(value: str, *, field_name: str, max_chars: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_chars
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field_name} is invalid")
