"""API Gateway WebSocket subscriptions and CockroachDB changefeed fanout."""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from hindsight.aws import aws_client_config
from hindsight.queueing import enqueue_run
from hindsight.realtime_ticket import verify_realtime_ticket
from hindsight.security import safe_error_detail

CONNECTION_TABLE_ENV = "HINDSIGHT_WEBSOCKET_CONNECTION_TABLE"
SUBSCRIPTION_TABLE_ENV = "HINDSIGHT_WEBSOCKET_SUBSCRIPTION_TABLE"
IDEMPOTENCY_TABLE_ENV = "HINDSIGHT_CHANGEFEED_IDEMPOTENCY_TABLE"
MANAGEMENT_ENDPOINT_ENV = "HINDSIGHT_WEBSOCKET_MANAGEMENT_ENDPOINT"
CHANGEFEED_TOKEN_ENV = "HINDSIGHT_CHANGEFEED_AUTH_TOKEN"
CHANGEFEED_TOKEN_PARAM_ENV = "HINDSIGHT_CHANGEFEED_AUTH_TOKEN_PARAM"
TICKET_SECRET_PARAM_ENV = "HINDSIGHT_REALTIME_TICKET_SECRET_PARAM"
CONNECTION_TTL_SECONDS = 24 * 60 * 60
EVENT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
EVENT_VERSION = 1
_CHANGEFEED_TOKEN_CACHE: str | None = None
_TICKET_SECRET_CACHE: str | None = None


def websocket_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle API Gateway WebSocket connect, disconnect, and subscription routes."""

    request_context = event.get("requestContext") or {}
    route = str(request_context.get("routeKey") or "$default")
    connection_id = str(request_context.get("connectionId") or "").strip()
    if not connection_id:
        return _response(400, {"error": "connection id is required"})
    table_name = os.environ.get(CONNECTION_TABLE_ENV)
    subscription_table_name = os.environ.get(SUBSCRIPTION_TABLE_ENV)
    if not table_name or not subscription_table_name:
        return _response(503, {"error": "connection registry is not configured"})
    dynamodb = boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    )
    table = dynamodb.Table(table_name)
    subscriptions = dynamodb.Table(subscription_table_name)
    try:
        if route == "$connect":
            ticket = str((event.get("queryStringParameters") or {}).get("ticket") or "")
            try:
                tenant_id = verify_realtime_ticket(ticket, secret=_ticket_secret())
            except ValueError as exc:
                return _response(401, {"error": str(exc)})
            table.put_item(
                Item={
                    "connection_id": connection_id,
                    "namespace": "",
                    "run_id": "",
                    "tenant_id": tenant_id,
                    "expires_at": int(time.time()) + CONNECTION_TTL_SECONDS,
                }
            )
            return _response(200, {"connected": True})
        if route == "$disconnect":
            _delete_subscriptions(subscriptions, connection_id)
            table.delete_item(Key={"connection_id": connection_id})
            return _response(200, {"connected": False})

        payload = _event_body(event)
        message_type = str(payload.get("type") or "").strip().lower()
        if message_type == "ping":
            return _response(200, {"type": "pong"})
        if message_type not in {"subscribe", "unsubscribe"}:
            return _response(400, {"error": "type must be subscribe, unsubscribe, or ping"})
        namespace = _optional_subscription(payload.get("namespace"), "namespace")
        run_id = _optional_subscription(payload.get("run_id"), "run_id")
        if message_type == "unsubscribe":
            namespace = ""
            run_id = ""
        connection = table.get_item(
            Key={"connection_id": connection_id},
            ConsistentRead=True,
        ).get("Item")
        if not connection:
            return _response(404, {"error": "connection is not registered"})
        tenant_id = str(connection.get("tenant_id") or "")
        if not tenant_id:
            return _response(403, {"error": "connection tenant is unavailable"})
        _delete_subscriptions(subscriptions, connection_id)
        expires_at = int(time.time()) + CONNECTION_TTL_SECONDS
        if message_type == "subscribe":
            topic_keys = []
            if namespace:
                topic_keys.append(f"tenant:{tenant_id}:namespace:{namespace}")
            if run_id:
                topic_keys.append(f"tenant:{tenant_id}:run:{run_id}")
            for topic_key in topic_keys:
                subscriptions.put_item(
                    Item={
                        "topic_key": topic_key,
                        "connection_id": connection_id,
                        "expires_at": expires_at,
                    }
                )
        table.update_item(
            Key={"connection_id": connection_id},
            UpdateExpression=(
                "SET #namespace = :namespace, run_id = :run_id, expires_at = :expires_at"
            ),
            ExpressionAttributeNames={"#namespace": "namespace"},
            ExpressionAttributeValues={
                ":namespace": namespace or "",
                ":run_id": run_id or "",
                ":expires_at": expires_at,
            },
            ConditionExpression="attribute_exists(connection_id)",
        )
        return _response(
            200,
            {"subscribed": message_type == "subscribe", "namespace": namespace, "run_id": run_id},
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return _response(400, {"error": str(exc)})
    except Exception as exc:
        return _response(500, {"error": safe_error_detail(exc)})


def changefeed_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Authenticate one webhook batch and fan normalized rows out to subscribers."""

    if not _changefeed_authorized(event):
        return _response(401, {"error": "invalid changefeed authorization"})
    try:
        body = _event_body(event)
        rows = body.get("payload", []) if isinstance(body, dict) else body
        if not isinstance(rows, list):
            raise ValueError("changefeed payload must be a list")
        accepted = 0
        delivered = 0
        stale = 0
        duplicates_ignored = 0
        consolidation_jobs = 0
        for row in rows:
            event_id = _outbox_event_id(row)
            if event_id is not None and not _claim_outbox_event(event_id):
                duplicates_ignored += 1
                continue
            try:
                transition = _resolved_incident_transition(row)
                if transition is not None:
                    enqueue_run(
                        {
                            "command": "consolidation",
                            "incident_id": transition["id"],
                            "source_event_id": transition["resolution_event_id"],
                            **(
                                {"tenant_id": transition["tenant_id"]}
                                if transition.get("tenant_id")
                                else {}
                            ),
                        }
                    )
                    consolidation_jobs += 1
                envelope = normalize_changefeed_row(row)
                if envelope is not None:
                    result = fanout_event(envelope)
                    accepted += 1
                    delivered += result["delivered"]
                    stale += result["stale"]
            except Exception:
                if event_id is not None:
                    _release_outbox_event(event_id)
                raise
        return _response(
            200,
            {
                "accepted": accepted,
                "delivered": delivered,
                "duplicates_ignored": duplicates_ignored,
                "stale_connections_removed": stale,
                "consolidation_jobs_queued": consolidation_jobs,
            },
        )
    except (ValueError, json.JSONDecodeError) as exc:
        return _response(400, {"error": str(exc)})
    except Exception as exc:
        return _response(500, {"error": safe_error_detail(exc)})


def normalize_changefeed_row(row: Any) -> dict[str, Any] | None:
    """Convert one CockroachDB webhook row to the product event envelope."""

    if not isinstance(row, dict) or row.get("resolved"):
        return None
    after = row.get("after")
    if after is None and isinstance(row.get("value"), dict):
        after = row["value"].get("after", row["value"])
    if not isinstance(after, dict):
        return None
    topic = str(row.get("topic") or row.get("table") or "").split(".")[-1]
    updated = row.get("updated") or after.get("updated_at") or datetime.now(UTC).isoformat()
    if topic == "tenant_event_outbox":
        aggregate_type = str(after.get("aggregate_type") or "")
        event_type = {
            "semantic_memories": "memory",
            "memory_operations": "operation",
            "agent_runs": "run",
            "agent_run_events": "run_event",
        }.get(aggregate_type)
        if event_type is None:
            return None
        event_id = str(after.get("id") or "")
        tenant_id = str(after.get("tenant_id") or "")
        if not event_id or not tenant_id:
            raise ValueError("outbox event identity is required")
        payload = after.get("payload") if isinstance(after.get("payload"), dict) else {}
        topics = after.get("topics") if isinstance(after.get("topics"), list) else []
        topic_keys = [str(value) for value in topics]
        if any(not value.startswith(f"tenant:{tenant_id}:") for value in topic_keys):
            raise ValueError("outbox topic tenant does not match event tenant")
        return {
            "version": EVENT_VERSION,
            "event_id": event_id,
            "tenant_id": tenant_id,
            "topic_keys": topic_keys,
            "type": event_type,
            "namespace": None,
            "run_id": str(payload.get("run_id")) if payload.get("run_id") else None,
            "occurred_at": str(updated),
            "data": {"reference": _jsonable(payload)},
        }
    namespace = after.get("namespace")
    run_id = after.get("run_id") or (after.get("id") if topic == "agent_runs" else None)

    if topic == "semantic_memories":
        event_type = "memory"
        data = {"memory": _normalize_memory(after)}
    elif topic == "memory_operations":
        event_type = "operation"
        data = {"operation": _normalize_operation(after)}
    elif topic == "agent_runs":
        event_type = "run"
        data = {"run": _jsonable(after)}
    elif topic == "agent_run_events":
        event_type = "run_event"
        data = {"run_event": _jsonable(after)}
    else:
        return None
    return {
        "version": EVENT_VERSION,
        "type": event_type,
        "namespace": namespace,
        "run_id": str(run_id) if run_id is not None else None,
        "occurred_at": str(updated),
        "data": data,
    }


def _resolved_incident_transition(row: Any) -> dict[str, str] | None:
    if not isinstance(row, dict):
        return None
    topic = str(row.get("topic") or row.get("table") or "").split(".")[-1]
    if topic == "tenant_event_outbox":
        after = row.get("after")
        if after is None and isinstance(row.get("value"), dict):
            after = row["value"].get("after", row["value"])
        if not isinstance(after, dict) or after.get("aggregate_type") != "incidents":
            return None
        payload = after.get("payload")
        if not isinstance(payload, dict):
            return None
        if payload.get("previous_status") == "resolved" or payload.get("status") != "resolved":
            return None
        if payload.get("consolidation_policy", "managed") != "managed":
            return None
        if not payload.get("incident_id") or not payload.get("resolution_event_id"):
            return None
        return {
            "id": str(payload["incident_id"]),
            "resolution_event_id": str(payload["resolution_event_id"]),
            "tenant_id": str(after.get("tenant_id") or ""),
        }
    if topic != "incidents":
        return None
    value = row.get("value") if isinstance(row.get("value"), dict) else row
    before = value.get("before") if isinstance(value, dict) else None
    after = value.get("after") if isinstance(value, dict) else None
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    if before.get("status") == "resolved" or after.get("status") != "resolved":
        return None
    if after.get("consolidation_policy", "managed") != "managed":
        # Manual producers (including the governed benchmark) own their
        # consolidation lifecycle and must not receive a second async claimant.
        return None
    if not after.get("id") or not after.get("resolution_event_id"):
        return None
    return {
        "id": str(after["id"]),
        "resolution_event_id": str(after["resolution_event_id"]),
    }


def _outbox_event_id(row: Any) -> str | None:
    if not isinstance(row, dict) or row.get("resolved"):
        return None
    topic = str(row.get("topic") or row.get("table") or "").split(".")[-1]
    if topic != "tenant_event_outbox":
        return None
    after = row.get("after")
    if after is None and isinstance(row.get("value"), dict):
        after = row["value"].get("after", row["value"])
    if not isinstance(after, dict) or not after.get("id"):
        raise ValueError("outbox event identity is required")
    return str(after["id"])


def fanout_event(
    envelope: dict[str, Any],
    *,
    table: Any | None = None,
    connection_table: Any | None = None,
    management_client: Any | None = None,
) -> dict[str, int]:
    """Send an event to matching subscriptions and remove HTTP 410 connections."""

    table = table or _subscription_table()
    connection_table = connection_table or _connection_table()
    management_client = management_client or _management_client()
    delivered = 0
    stale = 0
    subscriptions: dict[str, dict[str, Any]] = {}
    for topic_key in envelope.get("topic_keys") or []:
        response = table.query(KeyConditionExpression=Key("topic_key").eq(str(topic_key)))
        for subscription in response.get("Items", []):
            subscriptions[str(subscription["connection_id"])] = subscription
    for connection_id, subscription in subscriptions.items():
        if int(subscription.get("expires_at") or 0) <= int(time.time()):
            table.delete_item(
                Key={
                    "topic_key": subscription["topic_key"],
                    "connection_id": connection_id,
                }
            )
            continue
        try:
            management_client.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
            )
            delivered += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"GoneException", "410"}:
                raise
            _delete_subscriptions(table, connection_id)
            connection_table.delete_item(Key={"connection_id": connection_id})
            stale += 1
    return {"delivered": delivered, "stale": stale}


def _delete_subscriptions(table: Any, connection_id: str) -> None:
    response = table.query(
        IndexName="connection-id-index",
        KeyConditionExpression=Key("connection_id").eq(connection_id),
    )
    for item in response.get("Items", []):
        table.delete_item(
            Key={
                "topic_key": item["topic_key"],
                "connection_id": connection_id,
            }
        )


def _normalize_memory(row: dict[str, Any]) -> dict[str, Any]:
    memory = _jsonable(row)
    memory["status"] = "invalidated" if row.get("t_invalid") is not None else "current"
    return memory


def _normalize_operation(row: dict[str, Any]) -> dict[str, Any]:
    operation = _jsonable(row)
    operation["invalidated_memory_ids"] = row.get("invalidated_memory_ids") or []
    operation["restored_memory_ids"] = row.get("restored_memory_ids") or []
    return operation


def _changefeed_authorized(event: dict[str, Any]) -> bool:
    headers = {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}
    supplied = headers.get("authorization", "")
    expected = _changefeed_token()
    return bool(expected and supplied and hmac.compare_digest(supplied, f"Bearer {expected}"))


def _changefeed_token() -> str | None:
    global _CHANGEFEED_TOKEN_CACHE
    if _CHANGEFEED_TOKEN_CACHE is not None:
        return _CHANGEFEED_TOKEN_CACHE
    direct = os.environ.get(CHANGEFEED_TOKEN_ENV)
    if direct:
        _CHANGEFEED_TOKEN_CACHE = direct
        return direct
    parameter = os.environ.get(CHANGEFEED_TOKEN_PARAM_ENV)
    if not parameter:
        return None
    client = boto3.client(
        "ssm",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    )
    response = client.get_parameter(Name=parameter, WithDecryption=True)
    _CHANGEFEED_TOKEN_CACHE = str(response["Parameter"]["Value"])
    return _CHANGEFEED_TOKEN_CACHE


def _ticket_secret() -> str:
    global _TICKET_SECRET_CACHE
    if _TICKET_SECRET_CACHE is not None:
        return _TICKET_SECRET_CACHE
    parameter = os.environ.get(TICKET_SECRET_PARAM_ENV)
    if not parameter:
        raise ValueError("realtime ticket verification is not configured")
    response = boto3.client(
        "ssm",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).get_parameter(Name=parameter, WithDecryption=True)
    _TICKET_SECRET_CACHE = str(response["Parameter"]["Value"])
    return _TICKET_SECRET_CACHE


def _connection_table() -> Any:
    table_name = os.environ.get(CONNECTION_TABLE_ENV)
    if not table_name:
        raise RuntimeError(f"{CONNECTION_TABLE_ENV} is required")
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(table_name)


def _subscription_table() -> Any:
    table_name = os.environ.get(SUBSCRIPTION_TABLE_ENV)
    if not table_name:
        raise RuntimeError(f"{SUBSCRIPTION_TABLE_ENV} is required")
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(table_name)


def _idempotency_table() -> Any:
    table_name = os.environ.get(IDEMPOTENCY_TABLE_ENV)
    if not table_name:
        raise RuntimeError(f"{IDEMPOTENCY_TABLE_ENV} is required")
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(table_name)


def _claim_outbox_event(event_id: str, *, table: Any | None = None) -> bool:
    resolved_table = table or _idempotency_table()
    now = int(time.time())
    try:
        resolved_table.put_item(
            Item={
                "event_id": event_id,
                "expires_at": now + EVENT_IDEMPOTENCY_TTL_SECONDS,
            },
            ConditionExpression=(Attr("event_id").not_exists() | Attr("expires_at").lt(now)),
        )
    except resolved_table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    return True


def _release_outbox_event(event_id: str, *, table: Any | None = None) -> None:
    (table or _idempotency_table()).delete_item(Key={"event_id": event_id})


def _management_client() -> Any:
    endpoint = os.environ.get(MANAGEMENT_ENDPOINT_ENV)
    if not endpoint:
        raise RuntimeError(f"{MANAGEMENT_ENDPOINT_ENV} is required")
    return boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    )


def _event_body(event: dict[str, Any]) -> Any:
    body = event.get("body")
    if body is None:
        return {}
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode()
    if isinstance(body, (dict, list)):
        return body
    return json.loads(body)


def _optional_subscription(value: Any, name: str) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if len(result) > 500:
        raise ValueError(f"{name} must be at most 500 characters")
    return result or None


def _response(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload, sort_keys=True),
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return str(value) if not isinstance(value, (str, int, float, bool, type(None))) else value
