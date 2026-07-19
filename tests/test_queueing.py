"""Local and hosted run-command delivery checks."""

import json

import pytest


def test_inline_queue_uses_handler_and_retries_transient_batch_failure(monkeypatch):
    import hindsight.queueing as queueing
    import hindsight.worker as worker

    calls = []

    class ImmediateThread:
        def __init__(self, *, target, args, name, daemon):
            self.target = target
            self.args = args
            self.name = name
            assert daemon is True

        def start(self):
            self.target(*self.args)

    monkeypatch.delenv(queueing.RUN_QUEUE_URL_ENV, raising=False)
    monkeypatch.setenv(queueing.INLINE_WORKER_ENV, "1")
    monkeypatch.setattr(queueing.threading, "Thread", ImmediateThread)

    def process(message, *, dead_letter=False):
        calls.append((message, dead_letter))
        if len(calls) == 1:
            raise RuntimeError("transient local delivery")

    monkeypatch.setattr(worker, "process_message", process)
    monkeypatch.setattr(queueing, "INLINE_RETRY_SECONDS", 0)

    result = queueing.enqueue_run({"command": "start", "run_id": "run-1"})

    assert result == "inline:hindsight-run-run-1"
    assert calls == [
        (
            {
                "command": "start",
                "run_id": "run-1",
                "tenant_id": "00000000-0000-0000-0000-000000000002",
            },
            False,
        ),
        (
            {
                "command": "start",
                "run_id": "run-1",
                "tenant_id": "00000000-0000-0000-0000-000000000002",
            },
            False,
        ),
    ]


def test_queue_tenant_follows_bound_context_and_accepts_trusted_internal_messages(
    monkeypatch,
):
    import hindsight.queueing as queueing
    from hindsight.tenant import tenant_scope

    class FakeSqs:
        def __init__(self):
            self.messages = []

        def send_message(self, **kwargs):
            self.messages.append(kwargs)
            return {"MessageId": "message-1"}

    public = "00000000-0000-0000-0000-000000000002"
    acceptance = "00000000-0000-0000-0000-000000000003"
    client = FakeSqs()
    monkeypatch.setenv(queueing.RUN_QUEUE_URL_ENV, "https://sqs.example/queue")

    queueing.enqueue_run({"run_id": "acceptance", "tenant_id": acceptance}, client=client)
    assert json.loads(client.messages[-1]["MessageBody"])["tenant_id"] == acceptance

    with tenant_scope(public):
        with pytest.raises(RuntimeError, match="differs from the bound tenant"):
            queueing.enqueue_run(
                {"run_id": "wrong", "tenant_id": acceptance},
                client=client,
            )
