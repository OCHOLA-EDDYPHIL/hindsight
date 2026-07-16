"""Local and hosted run-command delivery checks."""


def test_inline_queue_uses_current_worker_message_interface(monkeypatch):
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
    monkeypatch.setattr(
        worker,
        "process_message",
        lambda message, *, dead_letter=False: calls.append((message, dead_letter)),
    )

    result = queueing.enqueue_run({"command": "start", "run_id": "run-1"})

    assert result == "inline:hindsight-run-run-1"
    assert calls == [({"command": "start", "run_id": "run-1"}, False)]
