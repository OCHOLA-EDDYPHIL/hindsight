"""Run-command delivery for hosted and local product API use."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any
from types import SimpleNamespace
from uuid import uuid4

import boto3

from hindsight.aws import aws_client_config
from hindsight.server_tenants import public_demo_tenant_id
from hindsight.tenant import current_tenant_id, normalize_tenant_id

RUN_QUEUE_URL_ENV = "HINDSIGHT_RUN_QUEUE_URL"
INLINE_WORKER_ENV = "HINDSIGHT_INLINE_WORKER"
INLINE_MAX_ATTEMPTS = 3
INLINE_RETRY_SECONDS = 0.1
INLINE_QUEUE_ARN = "local:sqs:hindsight-runs"
INLINE_DLQ_ARN = "local:sqs:hindsight-run-dlq"


class RunQueueUnavailableError(RuntimeError):
    """Raised when no hosted or local run executor is configured."""


def enqueue_run(message: dict[str, Any], *, client: Any | None = None) -> str:
    """Enqueue one run command, or execute it in a local background thread."""

    bound_tenant = current_tenant_id()
    supplied_tenant = message.get("tenant_id")
    normalized_supplied = (
        normalize_tenant_id(str(supplied_tenant)) if supplied_tenant is not None else None
    )
    if bound_tenant is not None and normalized_supplied not in {None, bound_tenant}:
        raise RuntimeError("internal queue tenant differs from the bound tenant")
    message = {
        **message,
        "tenant_id": bound_tenant or normalized_supplied or public_demo_tenant_id(),
    }
    carrier: dict[str, str] = {}
    try:
        from opentelemetry.propagate import inject
    except ImportError:
        inject = None
    if inject is not None:
        inject(carrier)
    if traceparent := carrier.get("traceparent"):
        message["traceparent"] = traceparent
    queue_url = os.environ.get(RUN_QUEUE_URL_ENV)
    if queue_url:
        resolved_client = client or boto3.client(
            "sqs",
            region_name=os.environ.get("AWS_REGION"),
            config=aws_client_config(read_timeout=10),
        )
        response = resolved_client.send_message(
            QueueUrl=queue_url,
            MessageBody=json.dumps(message, sort_keys=True),
        )
        return str(response["MessageId"])
    if _truthy(os.environ.get(INLINE_WORKER_ENV)):
        label = message.get("run_id") or message.get("operation_id") or "unknown"
        thread = threading.Thread(
            target=_run_inline_message,
            args=(message,),
            name=f"hindsight-run-{label}",
            daemon=True,
        )
        thread.start()
        return f"inline:{thread.name}"
    raise RunQueueUnavailableError(
        f"{RUN_QUEUE_URL_ENV} is not set and {INLINE_WORKER_ENV} is not enabled"
    )


def _run_inline_message(message: dict[str, Any]) -> None:
    from hindsight.worker import handler

    message_id = str(uuid4())
    for receive_count in range(1, INLINE_MAX_ATTEMPTS + 1):
        result = handler(
            _inline_event(
                message=message,
                message_id=message_id,
                receive_count=receive_count,
                source_arn=INLINE_QUEUE_ARN,
            ),
            SimpleNamespace(aws_request_id=f"inline:{uuid4()}"),
        )
        failed = {str(item.get("itemIdentifier")) for item in result.get("batchItemFailures", [])}
        if message_id not in failed:
            return
        if receive_count < INLINE_MAX_ATTEMPTS:
            time.sleep(INLINE_RETRY_SECONDS)
    handler(
        _inline_event(
            message=message,
            message_id=message_id,
            receive_count=1,
            source_arn=os.environ.get("HINDSIGHT_RUN_DLQ_ARN", INLINE_DLQ_ARN),
        ),
        SimpleNamespace(aws_request_id=f"inline-dlq:{uuid4()}"),
    )


def _inline_event(
    *,
    message: dict[str, Any],
    message_id: str,
    receive_count: int,
    source_arn: str,
) -> dict[str, Any]:
    return {
        "Records": [
            {
                "messageId": message_id,
                "body": json.dumps(message, sort_keys=True),
                "attributes": {"ApproximateReceiveCount": str(receive_count)},
                "eventSource": "aws:sqs",
                "eventSourceARN": source_arn,
            }
        ]
    }


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
