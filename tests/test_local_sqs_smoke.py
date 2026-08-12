"""Tests for the local AWS-compatible SQS contract smoke."""

from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

from hindsight.queueing import RUN_QUEUE_URL_ENV
from hindsight.server_tenants import public_demo_tenant_id


def _smoke_module():
    path = pathlib.Path("scripts/run_local_sqs_smoke.py")
    spec = importlib.util.spec_from_file_location("run_local_sqs_smoke", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSqs:
    def __init__(self, *, messages: list[dict[str, str]] | None = None):
        self.queue_url = "http://127.0.0.1:4566/queue/hindsight-local-smoke"
        self.messages = messages if messages is not None else []
        self.deleted: list[str] = []

    def create_queue(self, **_kwargs):
        return {"QueueUrl": self.queue_url}

    def send_message(self, **kwargs):
        self.messages.append({"Body": kwargs["MessageBody"]})
        return {"MessageId": "message-1"}

    def receive_message(self, **_kwargs):
        return {"Messages": list(self.messages)}

    def delete_queue(self, **kwargs):
        self.deleted.append(kwargs["QueueUrl"])


def test_local_sqs_smoke_uses_production_enqueue_and_cleans_up(monkeypatch):
    smoke = _smoke_module()
    client = FakeSqs()
    monkeypatch.setenv(RUN_QUEUE_URL_ENV, "previous-queue")

    result = smoke.run_smoke(client=client, queue_name="hindsight-local-smoke")

    assert result["message_id"] == "message-1"
    assert result["queue_name"] == "hindsight-local-smoke"
    assert json.loads(client.messages[0]["Body"]) == {
        "command": "start",
        "run_id": result["run_id"],
        "tenant_id": public_demo_tenant_id(),
    }
    assert client.deleted == [client.queue_url]
    assert smoke.os.environ[RUN_QUEUE_URL_ENV] == "previous-queue"


def test_local_sqs_smoke_cleans_up_when_no_message_arrives(monkeypatch):
    smoke = _smoke_module()
    client = FakeSqs()
    client.send_message = lambda **_kwargs: {"MessageId": "message-1"}
    monkeypatch.delenv(RUN_QUEUE_URL_ENV, raising=False)

    with pytest.raises(RuntimeError, match="expected one local queue message"):
        smoke.run_smoke(client=client, queue_name="hindsight-local-smoke")

    assert client.deleted == [client.queue_url]
    assert RUN_QUEUE_URL_ENV not in smoke.os.environ


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://sqs.us-east-1.amazonaws.com",
        "ftp://127.0.0.1:4566",
        "not-a-url",
    ],
)
def test_local_sqs_smoke_rejects_remote_or_invalid_endpoints(endpoint):
    smoke = _smoke_module()

    with pytest.raises(ValueError, match="endpoint|loopback|http"):
        smoke.require_loopback_endpoint(endpoint)
