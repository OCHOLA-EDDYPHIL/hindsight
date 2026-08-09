"""CockroachDB changefeed normalization and WebSocket delivery tests."""

import json
import re
import time
from types import SimpleNamespace

from botocore.exceptions import ClientError


class FakeConditionalCheckFailed(Exception):
    pass


class FakeLeaseTable:
    """Small stateful fake for the changefeed lease expressions."""

    def __init__(self, item=None):
        self.item = dict(item) if item else None
        self.updates = []
        self.reads = []
        self.meta = SimpleNamespace(
            client=SimpleNamespace(
                exceptions=SimpleNamespace(
                    ConditionalCheckFailedException=FakeConditionalCheckFailed
                )
            )
        )

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        values = kwargs["ExpressionAttributeValues"]
        expression = kwargs["UpdateExpression"]
        if "#state = :processing" in expression:
            now = values[":now"]
            claimable = self.item is None
            if self.item is not None:
                claimable = (
                    self.item.get("state") == "processing"
                    and self.item.get("lease_expires_at", now + 1) <= now
                ) or (
                    "state" not in self.item
                    and self.item.get("expires_at", now + 1) <= now
                )
            if not claimable:
                raise FakeConditionalCheckFailed()
            previous = self.item or {"event_id": kwargs["Key"]["event_id"]}
            self.item = {
                **previous,
                "state": "processing",
                "lease_owner": values[":owner"],
                "lease_expires_at": values[":lease_expires_at"],
                "attempt_count": int(previous.get("attempt_count", 0)) + 1,
                "started_at": previous.get("started_at", now),
                "updated_at": now,
                "expires_at": values[":expires_at"],
            }
            self.item.pop("completed_at", None)
            return {}
        if "#state = :completed" in expression:
            now = values[":now"]
            if not (
                self.item
                and self.item.get("state") == "processing"
                and self.item.get("lease_owner") == values[":owner"]
                and self.item.get("lease_expires_at", 0) > now
            ):
                raise FakeConditionalCheckFailed()
            self.item.update(
                state="completed",
                completed_at=now,
                updated_at=now,
                expires_at=values[":expires_at"],
            )
            self.item.pop("lease_owner", None)
            self.item.pop("lease_expires_at", None)
            return {}
        if "lease_expires_at = :expired" in expression:
            if not (
                self.item
                and self.item.get("state") == "processing"
                and self.item.get("lease_owner") == values[":owner"]
            ):
                raise FakeConditionalCheckFailed()
            self.item.update(
                lease_expires_at=values[":expired"],
                updated_at=values[":now"],
                expires_at=values[":expires_at"],
            )
            return {}
        raise AssertionError(f"unexpected update expression: {expression}")

    def get_item(self, **kwargs):
        self.reads.append(kwargs)
        return {"Item": dict(self.item)} if self.item is not None else {}


def test_websocket_connect_requires_a_valid_short_lived_ticket(monkeypatch):
    import hindsight.realtime as realtime
    from hindsight.realtime_ticket import issue_realtime_ticket

    class FakeTable:
        def __init__(self):
            self.puts = []

        def put_item(self, **kwargs):
            self.puts.append(kwargs["Item"])

    connections = FakeTable()
    subscriptions = FakeTable()
    resource = type(
        "Resource",
        (),
        {"Table": lambda self, name: connections if name == "connections" else subscriptions},
    )()
    monkeypatch.setenv(realtime.CONNECTION_TABLE_ENV, "connections")
    monkeypatch.setenv(realtime.SUBSCRIPTION_TABLE_ENV, "subscriptions")
    monkeypatch.setattr(realtime.boto3, "resource", lambda *args, **kwargs: resource)
    monkeypatch.setattr(realtime, "_TICKET_SECRET_CACHE", "ticket-secret")
    ticket = issue_realtime_ticket(
        tenant_id="00000000-0000-0000-0000-000000000003",
        secret="ticket-secret",
    )

    denied = realtime.websocket_handler(
        {"requestContext": {"routeKey": "$connect", "connectionId": "client-1"}},
        None,
    )
    accepted = realtime.websocket_handler(
        {
            "requestContext": {"routeKey": "$connect", "connectionId": "client-1"},
            "queryStringParameters": {"ticket": ticket},
        },
        None,
    )

    assert denied["statusCode"] == 401
    assert accepted["statusCode"] == 200
    assert connections.puts[0]["tenant_id"] == "00000000-0000-0000-0000-000000000003"


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

        def query(self, **kwargs):
            del kwargs
            return {
                "Items": [
                    {
                        "connection_id": "namespace-client",
                        "topic_key": "tenant:t1:namespace:demo:payments",
                        "expires_at": int(time.time()) + 60,
                    },
                    {
                        "connection_id": "run-client",
                        "topic_key": "tenant:t1:run:run-1",
                        "expires_at": int(time.time()) + 60,
                    },
                ]
            }

        def delete_item(self, **kwargs):
            self.deleted.append(kwargs["Key"])

    class FakeConnections:
        def __init__(self):
            self.deleted = []

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
    connections = FakeConnections()
    management = FakeManagement()
    result = fanout_event(
        {
            "version": 1,
            "type": "run_event",
            "namespace": "demo:payments",
            "run_id": "run-1",
            "topic_keys": [
                "tenant:t1:namespace:demo:payments",
                "tenant:t1:run:run-1",
            ],
            "occurred_at": "now",
            "data": {},
        },
        table=table,
        connection_table=connections,
        management_client=management,
    )

    assert result == {"delivered": 1, "stale": 1}
    assert management.sent[0][0] == "namespace-client"
    assert connections.deleted == ["run-client"]


def test_changefeed_handler_authenticates_and_accepts_batch(monkeypatch):
    import hindsight.realtime as realtime

    monkeypatch.setattr(realtime, "_CHANGEFEED_TOKEN_CACHE", "webhook-secret")
    delivered = []
    monkeypatch.setattr(
        realtime,
        "fanout_event",
        lambda envelope: delivered.append(envelope) or {"delivered": 2, "stale": 0},
    )
    monkeypatch.setattr(realtime, "_claim_outbox_event", lambda _event_id: True)
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
        "duplicates_ignored": 0,
        "stale_connections_removed": 0,
    }
    assert delivered[0]["type"] == "run"


def test_managed_changefeed_does_not_enqueue_manual_consolidation(monkeypatch):
    import hindsight.realtime as realtime

    monkeypatch.setattr(realtime, "_CHANGEFEED_TOKEN_CACHE", "webhook-secret")
    queued = []
    monkeypatch.setattr(realtime, "enqueue_run", lambda message: queued.append(message))
    monkeypatch.setattr(realtime, "fanout_event", lambda _envelope: {"delivered": 0, "stale": 0})

    response = realtime.changefeed_handler(
        {
            "headers": {"authorization": "Bearer webhook-secret"},
            "body": json.dumps(
                {
                    "payload": [
                        {
                            "topic": "incidents",
                            "value": {
                                "before": {"status": "open"},
                                "after": {
                                    "id": "incident-1",
                                    "slug": "benchmark:experiment:variant",
                                    "status": "resolved",
                                    "resolution_event_id": "event-1",
                                    "consolidation_policy": "manual",
                                },
                            },
                        }
                    ]
                }
            ),
        },
        None,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["consolidation_jobs_queued"] == 0
    assert queued == []


def test_resolved_transition_defaults_to_managed_consolidation():
    import hindsight.realtime as realtime

    transition = realtime._resolved_incident_transition(  # noqa: SLF001 - policy boundary
        {
            "topic": "incidents",
            "value": {
                "before": {"status": "open"},
                "after": {
                    "id": "incident-1",
                    "slug": "ordinary-incident",
                    "status": "resolved",
                    "resolution_event_id": "event-1",
                },
            },
        }
    )

    assert transition == {"id": "incident-1", "resolution_event_id": "event-1"}


def test_websocket_subscribe_updates_ephemeral_registry(monkeypatch):
    import hindsight.realtime as realtime

    class FakeTable:
        def __init__(self):
            self.updates = []
            self.puts = []

        def get_item(self, **kwargs):
            del kwargs
            return {
                "Item": {
                    "connection_id": "client-1",
                    "tenant_id": "00000000-0000-0000-0000-000000000002",
                }
            }

        def query(self, **kwargs):
            del kwargs
            return {"Items": []}

        def put_item(self, **kwargs):
            self.puts.append(kwargs["Item"])

        def update_item(self, **kwargs):
            self.updates.append(kwargs)

    connections = FakeTable()
    subscriptions = FakeTable()
    resource = type(
        "Resource",
        (),
        {"Table": lambda self, name: connections if name == "connections" else subscriptions},
    )()
    monkeypatch.setenv(realtime.CONNECTION_TABLE_ENV, "connections")
    monkeypatch.setenv(realtime.SUBSCRIPTION_TABLE_ENV, "subscriptions")
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
    values = connections.updates[0]["ExpressionAttributeValues"]
    assert values[":namespace"] == "demo:payments"
    assert values[":run_id"] == "run-1"
    assert {item["topic_key"] for item in subscriptions.puts} == {
        "tenant:00000000-0000-0000-0000-000000000002:namespace:demo:payments",
        "tenant:00000000-0000-0000-0000-000000000002:run:run-1",
    }


def test_outbox_changefeed_exposes_only_sanitized_references():
    from hindsight.realtime import normalize_changefeed_row

    envelope = normalize_changefeed_row(
        {
            "topic": "tenant_event_outbox",
            "updated": "1783960000000000000.0000000001",
            "after": {
                "id": "event-1",
                "tenant_id": "tenant-1",
                "aggregate_type": "semantic_memories",
                "topics": ["tenant:tenant-1:namespace:demo"],
                "payload": {"id": "memory-1", "status": "active"},
            },
        }
    )

    assert envelope == {
        "version": 2,
        "event_id": "event-1",
        "cursor": {
            "hlc": "1783960000000000000.0000000001",
            "event_id": "event-1",
        },
        "tenant_id": "tenant-1",
        "topic_keys": ["tenant:tenant-1:namespace:demo"],
        "type": "memory",
        "namespace": None,
        "run_id": None,
        "occurred_at": "1783960000000000000.0000000001",
        "data": {"reference": {"id": "memory-1", "status": "active"}},
    }


def test_duplicate_outbox_delivery_is_ignored(monkeypatch):
    import hindsight.realtime as realtime

    monkeypatch.setattr(realtime, "_CHANGEFEED_TOKEN_CACHE", "webhook-secret")
    claims = iter(
        [
            realtime.OutboxEventClaim("claimed", "owner-1"),
            realtime.OutboxEventClaim("completed"),
        ]
    )

    delivered = []
    monkeypatch.setattr(realtime, "_claim_outbox_event", lambda _event_id: next(claims))
    monkeypatch.setattr(realtime, "_complete_outbox_event", lambda *_args: True)
    monkeypatch.setattr(
        realtime,
        "fanout_event",
        lambda envelope: delivered.append(envelope) or {"delivered": 1, "stale": 0},
    )
    row = {
        "topic": "tenant_event_outbox",
        "updated": "1783960000000000000.0000000001",
        "after": {
            "id": "event-1",
            "tenant_id": "00000000-0000-0000-0000-000000000002",
            "aggregate_type": "semantic_memories",
            "topics": ["tenant:00000000-0000-0000-0000-000000000002:namespace:demo"],
            "payload": {"id": "memory-1"},
        },
    }
    event = {
        "headers": {"authorization": "Bearer webhook-secret"},
        "body": {"payload": [row]},
    }

    first = realtime.changefeed_handler(event, None)
    duplicate = realtime.changefeed_handler(event, None)

    assert json.loads(first["body"])["delivered"] == 1
    assert json.loads(duplicate["body"])["duplicates_ignored"] == 1
    assert len(delivered) == 1


def test_outbox_event_claim_takeover_and_completion_are_owner_fenced(monkeypatch):
    import hindsight.realtime as realtime

    monkeypatch.setenv(realtime.EVENT_LEASE_SECONDS_ENV, "60")
    table = FakeLeaseTable()

    first = realtime._claim_outbox_event("event-1", table=table, owner="owner-1", now=100)
    busy = realtime._claim_outbox_event("event-1", table=table, owner="owner-2", now=159)
    takeover = realtime._claim_outbox_event(
        "event-1", table=table, owner="owner-2", now=160
    )

    assert first == realtime.OutboxEventClaim("claimed", "owner-1")
    assert busy == realtime.OutboxEventClaim("busy")
    assert busy.owner is None
    assert table.reads[-1]["ConsistentRead"] is True
    assert takeover == realtime.OutboxEventClaim("claimed", "owner-2")
    assert table.item["attempt_count"] == 2
    assert realtime._complete_outbox_event(
        "event-1", "owner-1", table=table, now=161
    ) is False
    assert realtime._release_outbox_event(
        "event-1", "owner-1", table=table, now=161
    ) is False
    assert table.item["lease_owner"] == "owner-2"
    assert realtime._complete_outbox_event(
        "event-1", "owner-2", table=table, now=161
    ) is True
    assert table.item["state"] == "completed"
    assert realtime._claim_outbox_event(
        "event-1", table=table, owner="owner-3", now=162
    ) == realtime.OutboxEventClaim("completed")
    for update in table.updates:
        expressions = update["UpdateExpression"] + " " + update["ConditionExpression"]
        assert set(update["ExpressionAttributeValues"]) == set(
            re.findall(r":[a-z_]+", expressions)
        )
        assert set(update.get("ExpressionAttributeNames", {})) == set(
            re.findall(r"#[a-z_]+", expressions)
        )


def test_legacy_claim_fails_closed_until_its_existing_ttl_expires(monkeypatch):
    import hindsight.realtime as realtime

    monkeypatch.setenv(realtime.EVENT_LEASE_SECONDS_ENV, "60")
    table = FakeLeaseTable({"event_id": "event-1", "expires_at": 101})

    assert realtime._claim_outbox_event(
        "event-1", table=table, owner="owner-1", now=100
    ) == realtime.OutboxEventClaim("busy")
    assert realtime._claim_outbox_event(
        "event-1", table=table, owner="owner-1", now=101
    ) == realtime.OutboxEventClaim("claimed", "owner-1")


def test_active_outbox_lease_returns_retryable_503_without_projection(monkeypatch):
    import hindsight.realtime as realtime

    monkeypatch.setattr(realtime, "_CHANGEFEED_TOKEN_CACHE", "webhook-secret")
    monkeypatch.setenv(realtime.EVENT_LEASE_SECONDS_ENV, "60")
    table = FakeLeaseTable()
    realtime._claim_outbox_event("event-1", table=table, owner="owner-1", now=100)
    monkeypatch.setattr(realtime, "_idempotency_table", lambda: table)
    monkeypatch.setattr(realtime.time, "time", lambda: 120)
    projected = []
    monkeypatch.setattr(realtime, "fanout_event", lambda event: projected.append(event))

    response = realtime.changefeed_handler(_outbox_webhook_event(), None)

    assert response["statusCode"] == 503
    assert response["headers"]["retry-after"] == "1"
    assert json.loads(response["body"])["retryable"] is True
    assert projected == []


def test_projection_failure_releases_only_its_owner_for_immediate_retry(monkeypatch):
    import hindsight.realtime as realtime

    monkeypatch.setattr(realtime, "_CHANGEFEED_TOKEN_CACHE", "webhook-secret")
    monkeypatch.setenv(realtime.EVENT_LEASE_SECONDS_ENV, "60")
    monkeypatch.setattr(realtime.time, "time", lambda: 100)
    table = FakeLeaseTable()
    monkeypatch.setattr(realtime, "_idempotency_table", lambda: table)
    attempts = 0

    def fanout(_event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("delivery failed")
        return {"delivered": 1, "stale": 0}

    monkeypatch.setattr(realtime, "fanout_event", fanout)

    first = realtime.changefeed_handler(_outbox_webhook_event(), None)
    second = realtime.changefeed_handler(_outbox_webhook_event(), None)

    assert first["statusCode"] == 500
    assert second["statusCode"] == 200
    assert json.loads(second["body"])["delivered"] == 1
    assert attempts == 2
    assert table.item["state"] == "completed"
    assert table.item["attempt_count"] == 2


def test_outbox_completion_happens_only_after_projection(monkeypatch):
    import hindsight.realtime as realtime

    monkeypatch.setattr(realtime, "_CHANGEFEED_TOKEN_CACHE", "webhook-secret")
    order = []
    monkeypatch.setattr(
        realtime,
        "_claim_outbox_event",
        lambda _event_id: order.append("claim")
        or realtime.OutboxEventClaim("claimed", "owner-1"),
    )
    monkeypatch.setattr(
        realtime,
        "fanout_event",
        lambda _event: order.append("project") or {"delivered": 1, "stale": 0},
    )
    monkeypatch.setattr(
        realtime,
        "_complete_outbox_event",
        lambda *_args: order.append("complete") or True,
    )

    response = realtime.changefeed_handler(_outbox_webhook_event(), None)

    assert response["statusCode"] == 200
    assert order == ["claim", "project", "complete"]


def _outbox_webhook_event():
    return {
        "headers": {"authorization": "Bearer webhook-secret"},
        "body": {
            "payload": [
                {
                    "topic": "tenant_event_outbox",
                    "updated": "1783960000000000000.0000000001",
                    "after": {
                        "id": "event-1",
                        "tenant_id": "00000000-0000-0000-0000-000000000002",
                        "aggregate_type": "semantic_memories",
                        "topics": [
                            "tenant:00000000-0000-0000-0000-000000000002:namespace:demo"
                        ],
                        "payload": {"id": "memory-1"},
                    },
                }
            ]
        },
    }


def test_websocket_unsubscribe_and_disconnect_remove_indexed_subscriptions(monkeypatch):
    import hindsight.realtime as realtime

    class FakeConnections:
        def __init__(self):
            self.deleted = []
            self.updates = []

        def get_item(self, **kwargs):
            del kwargs
            return {
                "Item": {
                    "connection_id": "client-1",
                    "tenant_id": "00000000-0000-0000-0000-000000000002",
                }
            }

        def update_item(self, **kwargs):
            self.updates.append(kwargs)

        def delete_item(self, **kwargs):
            self.deleted.append(kwargs["Key"])

    class FakeSubscriptions:
        def __init__(self):
            self.deleted = []

        def query(self, **kwargs):
            assert kwargs["IndexName"] == "connection-id-index"
            return {
                "Items": [
                    {
                        "topic_key": "tenant:t1:namespace:demo",
                        "connection_id": "client-1",
                    }
                ]
            }

        def delete_item(self, **kwargs):
            self.deleted.append(kwargs["Key"])

    connections = FakeConnections()
    subscriptions = FakeSubscriptions()
    resource = type(
        "Resource",
        (),
        {"Table": lambda self, name: connections if name == "connections" else subscriptions},
    )()
    monkeypatch.setenv(realtime.CONNECTION_TABLE_ENV, "connections")
    monkeypatch.setenv(realtime.SUBSCRIPTION_TABLE_ENV, "subscriptions")
    monkeypatch.setattr(realtime.boto3, "resource", lambda *args, **kwargs: resource)

    unsubscribed = realtime.websocket_handler(
        {
            "requestContext": {"routeKey": "unsubscribe", "connectionId": "client-1"},
            "body": {"type": "unsubscribe"},
        },
        None,
    )
    disconnected = realtime.websocket_handler(
        {"requestContext": {"routeKey": "$disconnect", "connectionId": "client-1"}},
        None,
    )

    assert unsubscribed["statusCode"] == 200
    assert disconnected["statusCode"] == 200
    assert len(subscriptions.deleted) == 2
    assert connections.deleted == [{"connection_id": "client-1"}]
