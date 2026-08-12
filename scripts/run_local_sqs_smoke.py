"""Exercise the production SQS enqueue contract against a local endpoint."""

from __future__ import annotations

import argparse
import json
import os
from contextlib import contextmanager
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse
from uuid import uuid4

import boto3

from hindsight.aws import aws_client_config
from hindsight.queueing import RUN_QUEUE_URL_ENV, enqueue_run
from hindsight.server_tenants import public_demo_tenant_id

DEFAULT_ENDPOINT_URL = "http://127.0.0.1:4566"
DEFAULT_REGION = "us-east-1"


def require_loopback_endpoint(endpoint_url: str) -> str:
    """Return a safe local endpoint or reject remote AWS-compatible services."""

    parsed = urlparse(endpoint_url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("local AWS endpoint must use http or https")
    if hostname not in {"127.0.0.1", "localhost", "::1"} and not hostname.endswith(
        ".localhost.localstack.cloud"
    ):
        raise ValueError("local AWS smoke refuses non-loopback endpoints")
    return endpoint_url.rstrip("/")


def local_sqs_client(*, endpoint_url: str, region: str) -> Any:
    """Create an SQS client that cannot discover or use real AWS credentials."""

    return boto3.client(
        "sqs",
        endpoint_url=require_loopback_endpoint(endpoint_url),
        region_name=region,
        aws_access_key_id="test",
        aws_secret_access_key="test",
        aws_session_token="test",
        config=aws_client_config(read_timeout=10),
    )


@contextmanager
def queue_environment(queue_url: str) -> Iterator[None]:
    """Temporarily bind the production enqueue path to one local queue."""

    previous = os.environ.get(RUN_QUEUE_URL_ENV)
    os.environ[RUN_QUEUE_URL_ENV] = queue_url
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(RUN_QUEUE_URL_ENV, None)
        else:
            os.environ[RUN_QUEUE_URL_ENV] = previous


def validate_message(body: Mapping[str, Any], *, run_id: str) -> None:
    """Validate the minimum tenant-scoped run command sent by the product."""

    expected = {
        "command": "start",
        "run_id": run_id,
        "tenant_id": public_demo_tenant_id(),
    }
    if dict(body) != expected:
        raise RuntimeError(f"unexpected local queue message: {body!r}")


def run_smoke(*, client: Any, queue_name: str) -> dict[str, str]:
    """Create, exercise, and remove a temporary local SQS queue."""

    queue_url: str | None = None
    run_id = f"local-sqs-{uuid4().hex[:12]}"
    try:
        queue_url = str(client.create_queue(QueueName=queue_name)["QueueUrl"])
        with queue_environment(queue_url):
            message_id = enqueue_run(
                {"command": "start", "run_id": run_id},
                client=client,
            )
        response = client.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=5,
        )
        messages = response.get("Messages") or []
        if len(messages) != 1:
            raise RuntimeError(f"expected one local queue message, received {len(messages)}")
        body = json.loads(str(messages[0]["Body"]))
        if not isinstance(body, dict):
            raise RuntimeError("local queue message body must be an object")
        validate_message(body, run_id=run_id)
        return {
            "message_id": str(message_id),
            "queue_name": queue_name,
            "run_id": run_id,
        }
    finally:
        if queue_url is not None:
            client.delete_queue(QueueUrl=queue_url)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--endpoint-url",
        default=os.environ.get("HINDSIGHT_AWS_ENDPOINT_URL", DEFAULT_ENDPOINT_URL),
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", DEFAULT_REGION))
    args = parser.parse_args()
    queue_name = f"hindsight-local-smoke-{uuid4().hex[:12]}"
    result = run_smoke(
        client=local_sqs_client(endpoint_url=args.endpoint_url, region=args.region),
        queue_name=queue_name,
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
