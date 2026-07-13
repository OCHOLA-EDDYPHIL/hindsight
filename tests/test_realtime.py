"""CockroachDB changefeed normalization and WebSocket delivery tests."""

import json
import time

from botocore.exceptions import ClientError


def test_changefeed_rows_share_one_versioned_envelope():
    from hindsight.realtime import normalize_changefeed_row

    memory = normalize_changefeed_row(
        {
            "topic": "semantic_memories",
            "updated": "1783960000000000000.0000000000",
            "after": {
                "id": "memory-1",
                "namespace": "demo:payments",
                "content": "retry fanout",
                "t_invalid": None,
            },
        }
    )
    run_event = normalize_changefeed_row(
        {
            "topic": "agent_run_events",
            "after": {
                "id": "event-1",
                "run_id": "run-1",
                "phase": "recall",
                "status": "recalling",
            },
        }
    )

    assert memory == {
        "version": 1,
        "type": "memory",
        "namespace": "demo:payments",
        "run_id": None,
        "occurred_at": "1783960000000000000.0000000000",
        "data": {
            "memory": {
                "id": "memory-1",
                "namespace": "demo:payments",
                "content": "retry fanout",
                "t_invalid": None,
                "status": "current",
            }
        },
    }
    assert run_event["type"] == "run_event"
    assert run_event["run_id"] == "run-1"
    assert run_event["data"]["run_event"]["phase"] == "recall"


def test_fanout_matches_namespace_or_run_and_removes_gone_connections():
    from hindsight.realtime import fanout_event

    class FakeTable:
        def __init__(self):
            self.deleted = []

        def scan(self, **kwargs):
            return {
                "Items": [
                    {
                        "connection_id": "namespace-client",
                        "namespace": "demo:payments",
                        "run_id": "",
                        "expires_at": int(time.time()) + 60,
                    },
                    {
                        "connection_id": "run-client",
                        "namespace": "other",
                        "run_id": "run-1",
                        "expires_at": int(time.time()) + 60,
                    },
                    {
                        "connection_id": "ignored",
                        "namespace": "other",
                        "run_id": "run-2",
                        "expires_at": int(time.time()) + 60,
                    },
                ]
            }

        def delete_item(self, **kwargs):
            self.deleted.append(kwargs["Key"]["connection_id"])

    class FakeManagement:
        def __init__(self):
            self.sent = []

        def post_to_connection(self, *, ConnectionId, Data):
            if ConnectionId == "run-client":
                raise ClientError(
                    {"Error": {"Code": "GoneException", "Message": "gone"}},
                    "PostToConnection",
                )
            self.sent.append((ConnectionId, json.loads(Data)))

    table = FakeTable()
    management = FakeManagement()
    result = fanout_event(
        {
            "version": 1,
            "type": "run_event",
            "namespace": "demo:payments",
            "run_id": "run-1",
            "occurred_at": "now",
            "data": {},
        },
        table=table,
        management_client=management,
    )

    assert result == {"delivered": 1, "stale": 1}
    assert management.sent[0][0] == "namespace-client"
    assert table.deleted == ["run-client"]


def test_changefeed_handler_authenticates_and_accepts_batch(monkeypatch):
    import hindsight.realtime as realtime

    monkeypatch.setattr(realtime, "_CHANGEFEED_TOKEN_CACHE", "webhook-secret")
    delivered = []
    monkeypatch.setattr(
        realtime,
        "fanout_event",
        lambda envelope: delivered.append(envelope) or {"delivered": 2, "stale": 0},
    )
    event = {
        "headers": {"authorization": "Bearer webhook-secret"},
        "body": json.dumps(
            {
                "payload": [
                    {
                        "topic": "agent_runs",
                        "after": {
                            "id": "run-1",
                            "namespace": "demo:payments",
                            "status": "planning",
                        },
                    }
                ]
            }
        ),
    }

    response = realtime.changefeed_handler(event, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {
        "accepted": 1,
        "consolidation_jobs_queued": 0,
        "delivered": 2,
        "stale_connections_removed": 0,
    }
    assert delivered[0]["type"] == "run"


def test_websocket_subscribe_updates_ephemeral_registry(monkeypatch):
    import hindsight.realtime as realtime

    class FakeTable:
        def __init__(self):
            self.updates = []

        def update_item(self, **kwargs):
            self.updates.append(kwargs)

    table = FakeTable()
    resource = type("Resource", (), {"Table": lambda self, name: table})()
    monkeypatch.setenv(realtime.CONNECTION_TABLE_ENV, "connections")
    monkeypatch.setattr(realtime.boto3, "resource", lambda *args, **kwargs: resource)

    response = realtime.websocket_handler(
        {
            "requestContext": {"routeKey": "$default", "connectionId": "client-1"},
            "body": json.dumps(
                {"type": "subscribe", "namespace": "demo:payments", "run_id": "run-1"}
            ),
        },
        None,
    )

    assert response["statusCode"] == 200
    values = table.updates[0]["ExpressionAttributeValues"]
    assert values[":namespace"] == "demo:payments"
    assert values[":run_id"] == "run-1"
