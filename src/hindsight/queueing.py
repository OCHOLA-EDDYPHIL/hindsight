"""Run-command delivery for hosted and local product API use."""

from __future__ import annotations

import json
import os
import threading
from typing import Any

import boto3

from hindsight.aws import aws_client_config

RUN_QUEUE_URL_ENV = "HINDSIGHT_RUN_QUEUE_URL"
INLINE_WORKER_ENV = "HINDSIGHT_INLINE_WORKER"


class RunQueueUnavailableError(RuntimeError):
    """Raised when no hosted or local run executor is configured."""


def enqueue_run(message: dict[str, Any], *, client: Any | None = None) -> str:
    """Enqueue one run command, or execute it in a local background thread."""

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
        from hindsight.worker import process_message

        thread = threading.Thread(
            target=process_message,
            args=(message,),
            kwargs={"attempt": 1},
            name=f"hindsight-run-{message.get('run_id', 'unknown')}",
            daemon=True,
        )
        thread.start()
        return f"inline:{thread.name}"
    raise RunQueueUnavailableError(
        f"{RUN_QUEUE_URL_ENV} is not set and {INLINE_WORKER_ENV} is not enabled"
    )


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
