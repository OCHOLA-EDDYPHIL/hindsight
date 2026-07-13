"""Asynchronous run-worker behavior."""

from types import SimpleNamespace


def test_worker_records_progress_and_awaiting_approval(monkeypatch):
    import hindsight.worker as worker
    from hindsight.agent import IncidentAgentResult
    from hindsight.reasoning import DeterministicReasoningProvider

    run = {
        "id": "run-1",
        "thread_id": "thread-1",
        "decision_id": "agent:run-1:plan",
        "incident_slug": "checkout-latency",
        "namespace": "demo:payments",
        "service_slug": "payments-api",
        "user_input": "checkout p99 is above SLO",
    }
    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **kwargs: None)
    monkeypatch.setattr(worker, "claim_run", lambda **kwargs: run)
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
            reasoning_max_attempts=1,
        ),
    )
    monkeypatch.setattr(
        worker,
        "reasoning_provider_from_env",
        lambda env: DeterministicReasoningProvider(),
    )
    transitions = []
    monkeypatch.setattr(
        worker,
        "transition_run",
        lambda **kwargs: transitions.append(kwargs) or {"id": kwargs["run_id"], **kwargs},
    )

    def fake_agent(*args, **kwargs):
        kwargs["progress_callback"](
            "plan",
            "planning",
            {
                "plan": "throttle retries",
                "reasoning": {
                    "provider": "deterministic",
                    "model": "deterministic-v1",
                    "usage": {},
                },
            },
        )
        return IncidentAgentResult(
            thread_id="thread-1",
            interrupted=True,
            interrupt={"proposed_action": "review throttle"},
            state={},
            plan="throttle retries",
        )

    monkeypatch.setattr(worker, "run_incident_agent", fake_agent)

    result = worker.process_message({"command": "start", "run_id": "run-1"})

    assert [item["status"] for item in transitions] == ["planning", "awaiting_approval"]
    assert transitions[0]["fields"]["plan"] == "throttle retries"
    assert result["status"] == "awaiting_approval"


def test_worker_requeues_before_terminal_failure(monkeypatch):
    import hindsight.worker as worker
    from hindsight.reasoning import DeterministicReasoningProvider

    monkeypatch.setattr(worker, "configure_tracing_from_env", lambda **kwargs: None)
    monkeypatch.setattr(
        worker,
        "claim_run",
        lambda **kwargs: {
            "id": "run-1",
            "thread_id": "thread-1",
            "decision_id": "agent:run-1:plan",
            "incident_slug": "incident-1",
            "namespace": "demo:payments",
            "service_slug": None,
            "user_input": "latency",
        },
    )
    monkeypatch.setattr(
        worker,
        "runtime_settings",
        lambda: SimpleNamespace(
            database_url="postgresql://db",
            provider_env={"LLM_PROVIDER": "deterministic"},
            reasoning_max_attempts=1,
        ),
    )
    monkeypatch.setattr(
        worker,
        "reasoning_provider_from_env",
        lambda env: DeterministicReasoningProvider(),
    )
    monkeypatch.setattr(
        worker,
        "run_incident_agent",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("provider unavailable")),
    )
    transitions = []
    monkeypatch.setattr(worker, "transition_run", lambda **kwargs: transitions.append(kwargs) or {})

    try:
        worker.process_message({"command": "start", "run_id": "run-1"}, attempt=1)
    except RuntimeError:
        pass

    assert transitions[-1]["status"] == "queued"
    assert transitions[-1]["phase"] == "retry"
