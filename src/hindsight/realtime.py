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
from botocore.exceptions import ClientError

from hindsight.aws import aws_client_config
from hindsight.queueing import enqueue_run
from hindsight.security import safe_error_detail

CONNECTION_TABLE_ENV = "HINDSIGHT_WEBSOCKET_CONNECTION_TABLE"
MANAGEMENT_ENDPOINT_ENV = "HINDSIGHT_WEBSOCKET_MANAGEMENT_ENDPOINT"
CHANGEFEED_TOKEN_ENV = "HINDSIGHT_CHANGEFEED_AUTH_TOKEN"
CHANGEFEED_TOKEN_PARAM_ENV = "HINDSIGHT_CHANGEFEED_AUTH_TOKEN_PARAM"
CONNECTION_TTL_SECONDS = 24 * 60 * 60
EVENT_VERSION = 1
_CHANGEFEED_TOKEN_CACHE: str | None = None


def websocket_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle API Gateway WebSocket connect, disconnect, and subscription routes."""

    request_context = event.get("requestContext") or {}
    route = str(request_context.get("routeKey") or "$default")
    connection_id = str(request_context.get("connectionId") or "").strip()
    if not connection_id:
        return _response(400, {"error": "connection id is required"})
    table_name = os.environ.get(CONNECTION_TABLE_ENV)
    if not table_name:
        return _response(503, {"error": "connection registry is not configured"})
    table = boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(table_name)
    try:
        if route == "$connect":
            table.put_item(
                Item={
                    "connection_id": connection_id,
                    "namespace": "",
                    "run_id": "",
                    "expires_at": int(time.time()) + CONNECTION_TTL_SECONDS,
                }
            )
            return _response(200, {"connected": True})
        if route == "$disconnect":
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
        table.update_item(
            Key={"connection_id": connection_id},
            UpdateExpression=(
                "SET #namespace = :namespace, run_id = :run_id, expires_at = :expires_at"
            ),
            ExpressionAttributeNames={"#namespace": "namespace"},
            ExpressionAttributeValues={
                ":namespace": namespace or "",
                ":run_id": run_id or "",
                ":expires_at": int(time.time()) + CONNECTION_TTL_SECONDS,
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
        envelopes = [envelope for row in rows if (envelope := normalize_changefeed_row(row))]
        consolidation_jobs = 0
        for row in rows:
            transition = _resolved_incident_transition(row)
            if transition is None:
                continue
            enqueue_run(
                {
                    "command": "consolidation",
                    "incident_id": transition["id"],
                    "source_event_id": transition["resolution_event_id"],
                }
            )
            consolidation_jobs += 1
        delivered = 0
        stale = 0
        for envelope in envelopes:
            result = fanout_event(envelope)
            delivered += result["delivered"]
            stale += result["stale"]
        return _response(
            200,
            {
                "accepted": len(envelopes),
                "delivered": delivered,
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


def fanout_event(
    envelope: dict[str, Any],
    *,
    table: Any | None = None,
    management_client: Any | None = None,
) -> dict[str, int]:
    """Send an event to matching subscriptions and remove HTTP 410 connections."""

    table = table or _connection_table()
    management_client = management_client or _management_client()
    delivered = 0
    stale = 0
    for connection in _scan_connections(table):
        if not _subscription_matches(connection, envelope):
            continue
        connection_id = str(connection["connection_id"])
        try:
            management_client.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(),
            )
            delivered += 1
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"GoneException", "410"}:
                raise
            table.delete_item(Key={"connection_id": connection_id})
            stale += 1
    return {"delivered": delivered, "stale": stale}


def _scan_connections(table: Any):
    start_key = None
    while True:
        kwargs = {
            "ProjectionExpression": "connection_id, #namespace, run_id, expires_at",
            "ExpressionAttributeNames": {"#namespace": "namespace"},
        }
        if start_key:
            kwargs["ExclusiveStartKey"] = start_key
        response = table.scan(**kwargs)
        yield from response.get("Items", [])
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return


def _subscription_matches(connection: dict[str, Any], envelope: dict[str, Any]) -> bool:
    if int(connection.get("expires_at") or 0) <= int(time.time()):
        return False
    namespace = str(connection.get("namespace") or "")
    run_id = str(connection.get("run_id") or "")
    return bool(
        (namespace and namespace == str(envelope.get("namespace") or ""))
        or (run_id and run_id == str(envelope.get("run_id") or ""))
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


def _connection_table() -> Any:
    table_name = os.environ.get(CONNECTION_TABLE_ENV)
    if not table_name:
        raise RuntimeError(f"{CONNECTION_TABLE_ENV} is required")
    return boto3.resource(
        "dynamodb",
        region_name=os.environ.get("AWS_REGION"),
        config=aws_client_config(read_timeout=10),
    ).Table(table_name)


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
